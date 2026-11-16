import SwiftUI

/// Temporal Undo Map: what changed, and an honest answer to "can I take it back?".
///
/// The screen's whole value is that its "Undo" button is only offered where the
/// bytes to restore actually exist and still match. Everything else — a sent
/// email, a payment, an action on the physical world, a file somebody has edited
/// since — is shown as irreversible or conflicted, in words, rather than given a
/// button that would quietly do nothing or destroy newer work.
struct UndoMapView: View {
    @State private var timeline: APIModels.UndoTimeline?
    @State private var undone: APIModels.UndoResult?
    @State private var loading = false
    @State private var working: String?
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Undo Map",
                    subtitle: "What changed, when — and what can honestly be taken back."
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

                if let undone {
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: "arrow.uturn.backward.circle.fill")
                            .font(.system(size: 11)).foregroundColor(V.success)
                        Text(undone.restored)
                            .font(V.Font.body).foregroundColor(V.text)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(V.Space.m)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(V.surfaceAlt)
                    .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
                }

                if let timeline {
                    countsCard(timeline)
                    if timeline.entries.isEmpty {
                        Text("Nothing recorded yet. The map fills as actions that change files run.")
                            .font(V.Font.body).foregroundColor(V.textSecondary)
                    } else {
                        entryList(timeline)
                    }
                    Text(timeline.note)
                        .font(V.Font.caption).foregroundColor(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                } else if !loading {
                    Text("The undo map could not be read.")
                        .font(V.Font.body).foregroundColor(V.textSecondary)
                }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - Counts

    @ViewBuilder
    private func countsCard(_ t: APIModels.UndoTimeline) -> some View {
        HStack(spacing: V.Space.xl) {
            ForEach(["reversible", "conflicted", "irreversible", "undone", "unknown"],
                    id: \.self) { state in
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(t.counts[state] ?? 0)")
                        .font(V.Font.cardTitle).foregroundColor(color(state))
                    Text(state).font(V.Font.caption).foregroundColor(V.textTertiary)
                }
            }
            Spacer()
            Text("\(t.total) recorded").font(V.Font.caption).foregroundColor(V.textTertiary)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Entries

    @ViewBuilder
    private func entryList(_ t: APIModels.UndoTimeline) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("TIMELINE").font(V.Font.caption).foregroundColor(V.textTertiary)
            ForEach(t.entries) { entry in
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: symbol(entry.state))
                        .font(.system(size: 11)).foregroundColor(color(entry.state))
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: V.Space.s) {
                            Text(entry.tool).font(V.Font.label).foregroundColor(V.text)
                            Text(entry.path).font(V.Font.mono)
                                .foregroundColor(V.textSecondary).lineLimit(1)
                        }
                        Text(entry.why)
                            .font(V.Font.caption).foregroundColor(V.textTertiary)
                            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    if entry.state == "reversible" {
                        Button(working == entry.entry_id ? "Undoing…" : "Undo") {
                            Task { await undo(entry) }
                        }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(working != nil)
                    } else {
                        Text(entry.state)
                            .font(V.Font.caption).foregroundColor(color(entry.state))
                    }
                }
                .padding(V.Space.s)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(V.surfaceAlt)
                .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func symbol(_ state: String) -> String {
        switch state {
        case "reversible": return "arrow.uturn.backward.circle"
        case "undone": return "checkmark.circle"
        case "conflicted": return "exclamationmark.triangle"
        case "irreversible": return "lock.circle"
        default: return "questionmark.circle"
        }
    }

    private func color(_ state: String) -> Color {
        switch state {
        case "reversible": return V.accent
        case "undone": return V.success
        case "conflicted": return V.warning
        case "irreversible": return V.danger
        default: return V.textTertiary
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            timeline = try await api.get("/undo?limit=60", as: APIModels.UndoTimeline.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        loading = false
    }

    private func undo(_ entry: APIModels.UndoEntry) async {
        working = entry.entry_id
        errorMessage = nil
        do {
            undone = try await api.post("/undo/\(entry.entry_id)",
                                        body: Optional<EmptyBody>.none,
                                        as: APIModels.UndoResult.self)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        working = nil
    }
}

private struct EmptyBody: Codable {}