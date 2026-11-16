import SwiftUI

/// Recovery Lab: what went wrong, and the ranked safe ways to repair it.
///
/// The screen exists to make a recovery decision inspectable. The recovery
/// engine already tried *something* when a call failed; this shows every repair
/// it would consider, each with the rule that made it safe or unsafe, so a
/// person can see that a destructive action was never silently retried and that
/// the alternative chosen was chosen for a stated reason -- not a confidence
/// number invented to look decisive.
///
/// Two rules:
///
/// * **The ranking is advice, not a verdict.** The note says so, and the screen
///   does not colour a candidate green as if it had succeeded. Success is
///   decided afterwards by the same independent check as any run.
/// * **"No recommendation" is shown as its own answer.** A destructive failure
///   with no safe fallback reports that nothing is safe to try automatically,
///   rather than offering the least-bad thing dressed up as safe.
struct RecoveryLabView: View {
    @State private var failures: [APIModels.RecordedFailure] = []
    @State private var selected: APIModels.RecordedFailure?
    @State private var report: APIModels.TournamentReport?
    @State private var loading = false
    @State private var running = false
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Recovery Lab",
                    subtitle: "Every safe repair for a failure, ranked, with the reason."
                ) {
                    Button(loading ? "Loading…" : "Refresh") { Task { await load() } }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(loading)
                }

                if let errorMessage {
                    Text(errorMessage)
                        .font(V.Font.caption).foregroundColor(V.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }

                failureList

                if let report {
                    reportCard(report)
                } else if selected == nil && !failures.isEmpty {
                    Text("Pick a failure above to rank the safe repairs.")
                        .font(V.Font.body).foregroundColor(V.textSecondary)
                }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - Failures

    @ViewBuilder
    private var failureList: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("FAILURES").font(V.Font.caption).foregroundColor(V.textTertiary)

            if failures.isEmpty {
                Text(loading ? "Loading…"
                     : "No failures recorded. The failure ledger is empty -- which is "
                     + "an answer only when the read succeeded, and it did.")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(failures) { failure in
                    Button {
                        select(failure)
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                            Image(systemName: selected?.failure_id == failure.failure_id
                                  ? "largecircle.fill.circle" : "circle")
                                .font(.system(size: 11))
                                .foregroundColor(selected?.failure_id == failure.failure_id
                                                 ? V.accent : V.textTertiary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(failure.tool ?? "(no tool)")
                                    .font(V.Font.label).foregroundColor(V.text)
                                Text(failure.error ?? failure.note ?? "")
                                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                                    .lineLimit(1)
                            }
                            Spacer()
                            if failure.recovered == 1 {
                                Text("recovered").font(V.Font.caption)
                                    .foregroundColor(V.success)
                            } else {
                                Text(failure.strategy ?? "unrecovered")
                                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                            }
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
    private func reportCard(_ report: APIModels.TournamentReport) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            HStack {
                Text("TOURNAMENT").font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                Text(report.tool).font(V.Font.mono).foregroundColor(V.textTertiary)
            }

            Text(report.note)
                .font(V.Font.body).foregroundColor(V.text)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(report.candidates) { candidate in
                candidateRow(candidate, recommended: candidate.id == report.recommended?.id)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func candidateRow(_ c: APIModels.TournamentReport.Candidate,
                              recommended: Bool) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                Image(systemName: c.safety == "safe" ? "checkmark.shield" : "xmark.shield")
                    .font(.system(size: 11))
                    .foregroundColor(c.safety == "safe" ? V.success : V.danger)
                Text(kindLabel(c.kind)).font(V.Font.label).foregroundColor(V.text)
                Text(c.tool).font(V.Font.mono).foregroundColor(V.textSecondary)
                if recommended {
                    Text("try first")
                        .font(V.Font.caption).foregroundColor(V.accent)
                }
                Spacer()
                Text(c.safety)
                    .font(V.Font.caption)
                    .foregroundColor(c.safety == "safe" ? V.success : V.danger)
            }
            Text(c.safety_reason)
                .font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if !c.cost.isEmpty {
                Text("cost: \(c.cost)")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(c.safety == "safe" ? V.surfaceAlt : V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
    }

    private func kindLabel(_ kind: String) -> String {
        switch kind {
        case "retry": return "Retry"
        case "alternative": return "Fallback tool"
        case "ask": return "Ask the person"
        default: return kind
        }
    }

    // MARK: - Data

    private func select(_ failure: APIModels.RecordedFailure) {
        selected = failure
        report = nil
        Task { await runTournament(failure) }
    }

    private func runTournament(_ failure: APIModels.RecordedFailure) async {
        guard let tool = failure.tool, !tool.isEmpty else {
            errorMessage = "This failure names no tool, so there is nothing to rank."
            return
        }
        running = true
        errorMessage = nil
        do {
            report = try await api.post(
                "/recovery/tournament",
                body: APIModels.TournamentRequest(tool: tool,
                                                  error: failure.error ?? ""),
                as: APIModels.TournamentReport.self)
        } catch {
            report = nil
            errorMessage = error.localizedDescription
        }
        running = false
    }

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            failures = try await api.get("/failures?limit=40",
                                         as: [APIModels.RecordedFailure].self)
            // Rank the newest failure on open. The tournament is read-only, so
            // doing it unasked costs nothing and answers the question the person
            // came with rather than leaving a panel to click into.
            if selected == nil, let first = failures.first(where: {
                ($0.tool ?? "").isEmpty == false
            }) {
                select(first)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        loading = false
    }
}