import SwiftUI

/// Trust & Privacy: how much Vantage may do without asking, and the floor it
/// cannot go below.
///
/// The dial here is the point of Adaptive Autonomy Control: a single position
/// between "ask before everything" and "let a standing grant cover a repeat",
/// optionally narrowed to one project directory. What the screen must make
/// undeniable is the **ceiling** -- at any position, a destructive or physical
/// action is never auto-executed -- so it shows that sentence beside the dial
/// and then *proves* it with a live preview against a real tool.
///
/// The preview is the honest part. It runs the real classifier and the real
/// dial and shows the two verdicts side by side, so a person can see exactly
/// what moving the dial would change rather than trusting a description.
struct AutonomyControlView: View {
    @State private var posture: APIModels.AutonomyPosture?
    @State private var previewTool = "delete_file"
    @State private var preview: APIModels.AutonomyPreview?
    @State private var loading = false
    @State private var applying = false
    @State private var previewing = false
    @State private var scope = ""
    @State private var note = ""
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Autonomy Control",
                    subtitle: "How much may run without asking — and the floor no setting crosses."
                ) {
                    Button(loading ? "Refreshing…" : "Refresh") { Task { await load() } }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(loading)
                }

                if let errorMessage {
                    Text(errorMessage)
                        .font(V.Font.caption).foregroundColor(V.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let posture {
                    dialCard(posture)
                    ceilingCard(posture.ceiling)
                    previewCard(posture)
                } else if !loading {
                    Text("The autonomy posture could not be read.")
                        .font(V.Font.body).foregroundColor(V.textSecondary)
                }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - Dial

    @ViewBuilder
    private func dialCard(_ p: APIModels.AutonomyPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            HStack {
                Text("AUTONOMY").font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                Text(p.scope.isEmpty ? "everywhere" : p.scope)
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
            }

            ForEach(p.levels) { row in
                Button {
                    Task { await apply(level: row.level) }
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: row.level == p.level
                              ? "largecircle.fill.circle" : "circle")
                            .font(.system(size: 11))
                            .foregroundColor(row.level == p.level ? V.accent : V.textTertiary)
                        Text(row.name).font(V.Font.label).foregroundColor(V.text)
                        Spacer()
                        if row.level == p.level {
                            Text("current").font(V.Font.caption).foregroundColor(V.accent)
                        }
                    }
                    .contentShape(Rectangle())
                    .padding(.vertical, 3)
                }
                .buttonStyle(.plain)
                .disabled(applying)
            }

            Divider().opacity(0.4)

            TextField("Scope (optional) — e.g. /Users/you/Projects/lab",
                      text: $scope)
                .textFieldStyle(.roundedBorder)
                .font(V.Font.mono)
                .onAppear { scope = p.scope }

            HStack(spacing: V.Space.s) {
                Button(applying ? "Applying…" : "Apply scope") {
                    Task { await apply(level: p.level) }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(applying)
                Text("A dial with a scope applies only to actions whose every path is "
                     + "under that directory; a call that writes outside it is not scoped.")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - The ceiling

    @ViewBuilder
    private func ceilingCard(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Image(systemName: "lock.fill")
                .font(.system(size: 11)).foregroundColor(V.warning)
            Text(text)
                .font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Preview

    @ViewBuilder
    private func previewCard(_ p: APIModels.AutonomyPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            Text("WHAT WOULD THE DIAL DO?").font(V.Font.caption).foregroundColor(V.textTertiary)

            HStack(spacing: V.Space.s) {
                TextField("a tool name, e.g. delete_file", text: $previewTool)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.mono)
                    .onSubmit { Task { await runPreview() } }
                Button(previewing ? "Checking…" : "Preview") { Task { await runPreview() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(previewing || previewTool.trimmingCharacters(in: .whitespaces).isEmpty)
            }

            if let v = preview {
                HStack(alignment: .firstTextBaseline, spacing: V.Space.l) {
                    verdictBlock("Classifier says", v.baseline, muted: true)
                    Image(systemName: "arrow.right")
                        .font(.system(size: 10)).foregroundColor(V.textTertiary)
                    verdictBlock("With the dial", v.final, muted: false)
                    Spacer()
                    Text("risk \(v.risk) · \(v.risk_label)")
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                }
                if !v.stricter {
                    Text("The dial leaves this verdict where the classifier put it — "
                         + "either by design or because it is already as strict as it gets.")
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text("The preview runs the real classifier and the real dial without "
                     + "executing anything, so a destructive tool can be checked here "
                     + "and still not run.")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func verdictBlock(_ title: String, _ v: APIModels.AutonomyPreview.Verdict,
                              muted: Bool) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(V.Font.caption).foregroundColor(V.textTertiary)
            Text(v.decision)
                .font(V.Font.label)
                .foregroundColor(muted ? V.textSecondary : decisionColor(v.decision))
            Text(v.reason)
                .font(V.Font.caption).foregroundColor(V.textTertiary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func decisionColor(_ decision: String) -> Color {
        switch decision {
        case "EXECUTE": return V.success
        case "CONFIRM": return V.warning
        case "BLOCK": return V.danger
        default: return V.text
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            let p = try await api.get("/autonomy", as: APIModels.AutonomyPosture.self)
            posture = p
            scope = p.scope
            if preview == nil { await runPreview() }
        } catch {
            errorMessage = error.localizedDescription
        }
        loading = false
    }

    private func apply(level: Int) async {
        applying = true
        errorMessage = nil
        do {
            posture = try await api.post(
                "/autonomy",
                body: APIModels.AutonomySetRequest(level: level, scope: scope, note: note),
                as: APIModels.AutonomyPosture.self)
            await runPreview()
        } catch {
            errorMessage = error.localizedDescription
        }
        applying = false
    }

    private func runPreview() async {
        let tool = previewTool.trimmingCharacters(in: .whitespaces)
        guard !tool.isEmpty, let p = posture else { return }
        previewing = true
        do {
            preview = try await api.post(
                "/autonomy/preview",
                body: APIModels.AutonomyPreviewRequest(tool: tool, level: p.level,
                                                       params: [:]),
                as: APIModels.AutonomyPreview.self)
        } catch {
            preview = nil
            errorMessage = error.localizedDescription
        }
        previewing = false
    }
}