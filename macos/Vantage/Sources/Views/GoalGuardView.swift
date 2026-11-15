import SwiftUI

/// GoalGuard: the resources Vantage watches, and what it last saw.
///
/// Monitoring is opt-in and bounded, so this screen is explicit about three
/// things a person needs: which goals are enabled, whether each was actually
/// observed (and when), and whether the last look succeeded. A goal that has
/// never been observed says so rather than showing a reassuring green tick —
/// "not checked" and "healthy" are different states and this screen never
/// conflates them.
struct GoalGuardView: View {

    @State private var goals: [APIModels.MonitoredGoal] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var ticking = false
    @State private var lastTick: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "GoalGuard",
                           subtitle: "Resources Vantage watches, and when it last looked.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await load() } })
                }

                controls

                if goals.isEmpty && !isLoading && errorMessage == nil {
                    EmptyState(
                        symbol: "eye",
                        title: "Nothing is being watched",
                        message: "Monitoring is opt-in. No goal has been registered on "
                            + "this backend, so Vantage is looking at nothing.")
                }

                ForEach(goals) { goal in
                    goalRow(goal)
                }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
        .task { await load() }
    }

    private var controls: some View {
        HStack(spacing: V.Space.s) {
            Button(ticking ? "Checking…" : "Check what is due now") {
                Task { await tick() }
            }
            .buttonStyle(VPrimaryButtonStyle())
            .disabled(ticking)

            if let lastTick {
                Text(lastTick)
                    .font(V.Font.caption)
                    .foregroundColor(V.textSecondary)
            }
            Spacer()
        }
    }

    private func goalRow(_ goal: APIModels.MonitoredGoal) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(spacing: V.Space.s) {
                Text(goal.kind.uppercased())
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                    .frame(width: 44, alignment: .leading)
                Text(goal.target)
                    .font(V.Font.mono)
                    .foregroundColor(V.text)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                stateBadge(goal)
            }

            HStack(spacing: V.Space.m) {
                Text(observedText(goal))
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                if let detail = goal.last_detail, !detail.isEmpty {
                    Text(detail)
                        .font(V.Font.caption)
                        .foregroundColor(V.textSecondary)
                        .lineLimit(1)
                }
                Spacer()
                if goal.consecutive_failures > 0 {
                    Text("\(goal.consecutive_failures) consecutive failures")
                        .font(V.Font.caption)
                        .foregroundColor(V.warning)
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
        .overlay(RoundedRectangle(cornerRadius: V.Radius.card)
            .stroke(V.border, lineWidth: 1))
    }

    /// The badge says what was *observed*, not what was hoped for.
    private func stateBadge(_ goal: APIModels.MonitoredGoal) -> some View {
        let (label, tone): (String, Color)
        if !goal.enabled {
            (label, tone) = ("disabled", V.textTertiary)
        } else if goal.last_result == nil || goal.last_result == "" {
            (label, tone) = ("never observed", V.textTertiary)
        } else {
            switch goal.last_result {
            case "verified": (label, tone) = ("verified", V.success)
            case "failed": (label, tone) = ("failed", V.danger)
            default: (label, tone) = ("unknown", V.warning)
            }
        }
        return Text(label)
            .font(V.Font.caption)
            .foregroundColor(tone)
            .padding(.horizontal, V.Space.s)
            .padding(.vertical, V.Space.xxs)
            .background(tone.opacity(0.1))
            .clipShape(Capsule())
    }

    private func observedText(_ goal: APIModels.MonitoredGoal) -> String {
        guard let epoch = goal.last_observed_at, epoch > 0 else {
            return "every \(Int(goal.interval_seconds))s · not yet checked"
        }
        let seconds = Int(Date().timeIntervalSince1970 - epoch)
        let ago = seconds < 60 ? "\(seconds)s ago" : "\(seconds / 60)m ago"
        return "every \(Int(goal.interval_seconds))s · last checked \(ago) · "
            + "\(goal.run_count) observations"
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            let response: APIModels.GoalList = try await api.get("/intelligence/goals")
            goals = response.goals
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func tick() async {
        ticking = true
        errorMessage = nil
        do {
            let response: APIModels.GoalTick = try await api.post(
                "/intelligence/goals/tick", body: Optional<String>.none,
                as: APIModels.GoalTick.self)
            lastTick = response.observations.isEmpty
                ? "Nothing was due."
                : "Observed \(response.observations.count) goal(s)."
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        ticking = false
    }
}