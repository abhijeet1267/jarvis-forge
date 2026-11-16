import SwiftUI

/// Capability Passports: a worker's standing, as a record rather than a credential.
///
/// The screen exists to answer two questions a capability list never does: *what
/// has this worker actually done*, and *what does it hold that it has never
/// touched*. The second is the load-bearing one — a destructive tool with no
/// recorded use is a blast radius that is entirely theoretical, and no summary
/// that only counts tools would say so.
///
/// Every passport carries `authority`, which states in the backend's own words
/// that the document grants nothing; it is rendered, not paraphrased, so the
/// caveat cannot drift from the one the API asserts.
struct CapabilityPassportsView: View {
    @State private var workers: [APIModels.PassportSummary] = []
    @State private var listNote = ""
    @State private var passport: APIModels.WorkerPassport?
    @State private var selected: String?
    @State private var loading = false
    @State private var issuing = false

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Capability Passports",
                    subtitle: "What each worker holds, has done, and has never touched."
                ) {
                    Button(loading ? "Reading…" : "Refresh") { Task { await load() } }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(loading)
                }

                if !listNote.isEmpty {
                    Text(listNote).font(V.Font.caption).foregroundColor(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let passport {
                    passportCard(passport)
                } else if !loading {
                    Text("Select a worker.").font(V.Font.body)
                        .foregroundColor(V.textSecondary)
                }

                workerList
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - The passport

    @ViewBuilder
    private func passportCard(_ p: APIModels.WorkerPassport) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                Text(p.worker).font(V.Font.pageTitle).foregroundColor(V.text)
                Spacer()
                Button(issuing ? "Recording…" : "Record in audit log") {
                    Task { await issue(p.worker) }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(issuing)
            }

            // The authority line, verbatim from the backend.
            HStack(alignment: .top, spacing: V.Space.s) {
                Image(systemName: "info.circle")
                    .font(.system(size: 11)).foregroundColor(V.warning)
                Text(p.authority).font(V.Font.caption).foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack(spacing: V.Space.xl) {
                metric("\(p.tools.count)", "tools")
                metric("\(p.gated_tools.count)", "gated", V.warning)
                metric("\(p.exercised.count)", "exercised", V.success)
                metric("\(p.never_used.count)", "never used", V.textTertiary)
                metric("\(p.actions_total)", "actions")
                Spacer()
                evidenceBadge(p.evidence)
            }

            if !p.actions_by_risk.isEmpty {
                HStack(spacing: V.Space.l) {
                    Text("BY RISK").font(V.Font.caption).foregroundColor(V.textTertiary)
                    ForEach(p.actions_by_risk.keys.sorted(), id: \.self) { tier in
                        Text("\(tier) \(p.actions_by_risk[tier] ?? 0)")
                            .font(V.Font.caption)
                            .foregroundColor(tier == "read" ? V.textSecondary : V.warning)
                    }
                }
            }

            ForEach(p.notes, id: \.self) { note in
                HStack(alignment: .top, spacing: V.Space.s) {
                    Image(systemName: noteSymbol(note))
                        .font(.system(size: 11)).foregroundColor(noteColor(note))
                    Text(note).font(V.Font.caption).foregroundColor(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            toolsTable(p)
        }
        .padding(V.Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func evidenceBadge(_ e: APIModels.WorkerPassport.Evidence) -> some View {
        HStack(spacing: V.Space.xs) {
            Image(systemName: e.intact ? "checkmark.seal" : "exclamationmark.triangle")
                .font(.system(size: 11))
                .foregroundColor(e.intact ? V.success : V.danger)
            Text(e.intact
                 ? "audit chain verified · \(e.checked) record\(e.checked == 1 ? "" : "s")"
                 : "audit chain broken · \(e.breaks) break(s)")
                .font(V.Font.caption)
                .foregroundColor(e.intact ? V.textTertiary : V.danger)
        }
    }

    @ViewBuilder
    private func metric(_ value: String, _ label: String, _ color: Color = V.text) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(V.Font.cardTitle).foregroundColor(color)
            Text(label).font(V.Font.caption).foregroundColor(V.textTertiary)
        }
    }

    @ViewBuilder
    private func toolsTable(_ p: APIModels.WorkerPassport) -> some View {
        let neverUsed = Set(p.never_used)
        VStack(alignment: .leading, spacing: V.Space.xs) {
            Text("TOOLS").font(V.Font.caption).foregroundColor(V.textTertiary)
            ForEach(p.tools) { tool in
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Circle()
                        .fill(neverUsed.contains(tool.tool) ? V.textTertiary : V.success)
                        .frame(width: 5, height: 5)
                    Text(tool.tool).font(V.Font.mono).foregroundColor(V.text)
                        .lineLimit(1)
                    Text(tool.risk).font(V.Font.caption)
                        .foregroundColor(tool.risk == "read" ? V.textTertiary : V.warning)
                    if tool.requires_confirmation {
                        Text("needs approval").font(V.Font.caption)
                            .foregroundColor(V.textTertiary)
                    }
                    Spacer()
                    if neverUsed.contains(tool.tool) {
                        Text("never used").font(V.Font.caption).foregroundColor(V.textTertiary)
                    }
                }
                .padding(.vertical, 1)
            }
        }
    }

    // MARK: - The list

    @ViewBuilder
    private var workerList: some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            Text("WORKERS").font(V.Font.caption).foregroundColor(V.textTertiary)
            ForEach(workers) { w in
                Button {
                    Task { await inspect(w.worker) }
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: selected == w.worker
                              ? "circle.inset.filled" : "circle")
                            .font(.system(size: 10))
                            .foregroundColor(selected == w.worker ? V.accent : V.textTertiary)
                        Text(w.worker).font(V.Font.label).foregroundColor(V.text)
                        Spacer()
                        if let error = w.error {
                            Text(error).font(V.Font.caption).foregroundColor(V.danger)
                                .lineLimit(1)
                        } else {
                            Text("\(w.tools ?? 0) tools · \(w.gated ?? 0) gated · "
                                 + "\(w.never_used ?? 0) never used · \(w.actions_total ?? 0) actions")
                                .font(V.Font.caption).foregroundColor(V.textTertiary)
                            if w.evidence_intact == false {
                                Image(systemName: "exclamationmark.triangle")
                                    .font(.system(size: 10)).foregroundColor(V.danger)
                            }
                        }
                    }
                    .padding(V.Space.s)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(selected == w.worker ? V.surfaceAlt : Color.clear)
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

    private func noteSymbol(_ note: String) -> String {
        note.contains("did not re-walk clean") ? "exclamationmark.triangle" : "info.circle"
    }

    private func noteColor(_ note: String) -> Color {
        note.contains("did not re-walk clean") ? V.danger : V.textTertiary
    }

    // MARK: - Data

    private func load() async {
        loading = true
        do {
            let list = try await api.get("/passports", as: APIModels.PassportList.self)
            workers = list.workers
            listNote = list.note
        } catch {
            listNote = "The passport list could not be read."
        }
        loading = false
        if selected == nil, let first = workers.first(where: { $0.error == nil }) {
            await inspect(first.worker)
        }
    }

    private func inspect(_ worker: String) async {
        selected = worker
        do {
            passport = try await api.get("/passports/\(worker)",
                                         as: APIModels.WorkerPassport.self)
        } catch {
            passport = nil
        }
    }

    private func issue(_ worker: String) async {
        issuing = true
        do {
            passport = try await api.post("/passports/\(worker)/issue",
                                          body: Optional<IssueBody>.none,
                                          as: APIModels.WorkerPassport.self)
        } catch {
            // A refusal leaves the passport as it was rather than showing a
            // record that was never written.
        }
        issuing = false
    }
}

private struct IssueBody: Codable {}