import SwiftUI

/// Outcome Assurance: what can be checked after the fact, and a live check of
/// the world that proves the check is real.
///
/// A mission's success conditions were all file conditions — a file contains
/// this, hashes to that, is gone. Everything else could only be recorded
/// UNKNOWN. This screen shows the observers that answer questions about a page,
/// a command, a process, a port or the frontmost app, and lets a person run one
/// now, so the answer is not taken on faith.
///
/// The line the screen keeps is the module's rule: a check that **could not
/// run** is UNKNOWN, never a pass. So a blocked address reads "unknown" with the
/// reason, not a green tick — and the three states are never collapsed.
struct OutcomeAssuranceView: View {
    @State private var kinds: [APIModels.AssuranceKind] = []
    @State private var rule = ""
    @State private var spec = #"{"kind": "port_listening", "port": 8765}"#
    @State private var observation: APIModels.AssuranceObservation?
    @State private var loading = false
    @State private var checking = false
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Outcome Assurance",
                    subtitle: "What can be checked after the fact — and a live check to prove it."
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

                if !rule.isEmpty { ruleCard }

                checkCard
                kindList
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    @ViewBuilder
    private var ruleCard: some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Image(systemName: "checkmark.seal").font(.system(size: 11))
                .foregroundColor(V.accent)
            Text(rule)
                .font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Live check

    @ViewBuilder
    private var checkCard: some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            Text("RUN AN OBSERVATION NOW")
                .font(V.Font.caption).foregroundColor(V.textTertiary)

            TextEditor(text: $spec)
                .font(V.Font.mono)
                .frame(minHeight: 52, maxHeight: 90)
                .padding(V.Space.xs)
                .background(V.surfaceAlt)
                .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))

            HStack(spacing: V.Space.s) {
                Button(checking ? "Observing…" : "Observe") { Task { await run() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(checking)
                if let observation {
                    Image(systemName: symbol(observation.status))
                        .font(.system(size: 11)).foregroundColor(color(observation.status))
                    Text(observation.status.uppercased())
                        .font(V.Font.label).foregroundColor(color(observation.status))
                    Text(observation.detail)
                        .font(V.Font.caption).foregroundColor(V.textSecondary)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("Only the command observer runs a process, and only from its "
                         + "allowlist; everything else is a read.")
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Kinds

    @ViewBuilder
    private var kindList: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("WHAT CAN BE CHECKED").font(V.Font.caption).foregroundColor(V.textTertiary)
            ForEach(kinds) { k in
                Button {
                    spec = template(for: k.kind)
                    observation = nil
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Text(k.kind).font(V.Font.mono).foregroundColor(V.text)
                            .frame(width: 130, alignment: .leading)
                        Text(k.question)
                            .font(V.Font.caption).foregroundColor(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer()
                        Text(k.carries)
                            .font(V.Font.caption).foregroundColor(V.textTertiary)
                            .lineLimit(1)
                    }
                    .contentShape(Rectangle())
                    .padding(.vertical, 3)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func template(for kind: String) -> String {
        switch kind {
        case "url_body": return #"{"kind": "url_body", "url": "https://en.wikipedia.org/", "needle": "Wikipedia"}"#
        case "url_status": return #"{"kind": "url_status", "url": "https://127.0.0.1:8765/health", "codes": [200]}"#
        case "command_output": return #"{"kind": "command_output", "command": "uname", "needle": "Darwin"}"#
        case "process_running": return #"{"kind": "process_running", "process": "launchd"}"#
        case "port_listening": return #"{"kind": "port_listening", "port": 8765}"#
        case "app_frontmost": return #"{"kind": "app_frontmost", "app": "Finder"}"#
        default: return #"{"kind": "\#(kind)"}"#
        }
    }

    private func symbol(_ status: String) -> String {
        switch status {
        case "verified": return "checkmark.seal.fill"
        case "failed": return "xmark.seal.fill"
        default: return "questionmark.circle"
        }
    }

    private func color(_ status: String) -> Color {
        switch status {
        case "verified": return V.success
        case "failed": return V.danger
        default: return V.warning
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            let body = try await api.get("/assurance", as: APIModels.AssuranceKinds.self)
            kinds = body.kinds
            rule = body.rule
            if observation == nil { await run() }
        } catch {
            errorMessage = error.localizedDescription
        }
        loading = false
    }

    private func run() async {
        checking = true
        errorMessage = nil
        do {
            observation = try await api.post(
                "/assurance/observe",
                body: APIModels.AssuranceObserveRequest(spec: spec),
                as: APIModels.AssuranceObservation.self)
        } catch {
            observation = nil
            errorMessage = error.localizedDescription
        }
        checking = false
    }
}