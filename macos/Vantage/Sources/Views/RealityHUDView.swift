import SwiftUI

/// Reality HUD: what this Mac actually looks like right now, and a takeover that bites.
///
/// Two halves, and both refuse to be flattering. The HUD shows permission states
/// exactly as the preflight reported them — an unaskable permission reads
/// `UNKNOWN`, never a reassuring colour — and says whether those values describe
/// Vantage.app or the process that asked. The takeover half calls the pause gate
/// that every tool call passes through, so "taken over" means the next dispatch
/// is refused and counted; the screen states, in the backend's own words, that a
/// call already inside a handler is not interrupted.
struct RealityHUDView: View {
    @State private var hud: APIModels.RealityHUD?
    @State private var taken: APIModels.TakeoverResult?
    @State private var released: APIModels.ReleaseResult?
    @State private var loading = false
    @State private var working = false
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Reality HUD",
                    subtitle: "What is really on this Mac, and a pause that refuses the next call."
                ) {
                    Button(loading ? "Reading…" : "Refresh") { Task { await load() } }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(loading)
                }

                if let errorMessage {
                    Text(errorMessage).font(V.Font.caption).foregroundColor(V.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let hud {
                    takeoverCard(hud)
                    permissionsCard(hud.permissions)
                    workersCard(hud.workers)
                    Text(hud.honest).font(V.Font.caption).foregroundColor(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                } else if !loading {
                    Text("The machine could not be read.").font(V.Font.body)
                        .foregroundColor(V.textSecondary)
                }

                if let taken { outcomeCard(taken) }
                if let released { releasedCard(released) }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - Takeover

    @ViewBuilder
    private func takeoverCard(_ hud: APIModels.RealityHUD) -> some View {
        let held = hud.takeover.held
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                Image(systemName: held ? "hand.raised.fill" : "hand.raised.slash")
                    .font(.system(size: 13))
                    .foregroundColor(held ? V.warning : V.textTertiary)
                Text(held ? "Taken over — agents will not run" : "Agents may run")
                    .font(V.Font.sectionTitle).foregroundColor(V.text)
                Spacer()
                if held {
                    Button(working ? "Releasing…" : "Hand control back") {
                        Task { await release() }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(working)
                } else {
                    Button(working ? "Taking over…" : "Take over now") {
                        Task { await takeOver() }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(working)
                }
            }
            Text("Enforced at \(hud.takeover.enforced_at). It cannot \(hud.takeover.cannot).")
                .font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if held && !hud.takeover.held_workers.isEmpty {
                Text(hud.takeover.held_workers.joined(separator: " · "))
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
                    .lineLimit(2)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func outcomeCard(_ r: APIModels.TakeoverResult) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            Text("Paused \(r.paused.count) worker(s)").font(V.Font.label)
                .foregroundColor(V.text)
            Text(r.effect).font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Text("Not interrupted: \(r.cannot).").font(V.Font.caption)
                .foregroundColor(V.warning)
                .fixedSize(horizontal: false, vertical: true)
            if !r.already_paused_by_someone_else.isEmpty {
                Text("Left alone (paused by somebody else): "
                     + r.already_paused_by_someone_else.joined(separator: ", "))
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func releasedCard(_ r: APIModels.ReleaseResult) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            Text("Resumed \(r.resumed.count) worker(s)").font(V.Font.label)
                .foregroundColor(V.text)
            ForEach(r.left_paused, id: \.worker) { left in
                Text("\(left.worker) — \(left.why)").font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(r.note).font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Permissions

    @ViewBuilder
    private func permissionsCard(_ p: APIModels.RealityHUD.Permissions) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(spacing: V.Space.s) {
                Text("PERMISSIONS").font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                Text(p.about_bundled_host
                     ? "read by Vantage.app"
                     : "read by another process — not Vantage.app")
                    .font(V.Font.caption)
                    .foregroundColor(p.about_bundled_host ? V.textTertiary : V.warning)
            }
            ForEach(p.rows) { row in
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: symbol(row.status))
                        .font(.system(size: 11)).foregroundColor(color(row.status))
                    Text(row.label.isEmpty ? row.capability : row.label)
                        .font(V.Font.label).foregroundColor(V.text)
                    Spacer()
                    Text(row.status).font(V.Font.caption).foregroundColor(color(row.status))
                }
                .padding(.vertical, 2)
            }
            if !p.readable {
                Text(p.note).font(V.Font.caption).foregroundColor(V.danger)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(p.note).font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Workers

    @ViewBuilder
    private func workersCard(_ w: APIModels.RealityHUD.Workers) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(spacing: V.Space.l) {
                Text("WORKERS").font(V.Font.caption).foregroundColor(V.textTertiary)
                metric("\(w.running)", "running", w.running > 0 ? V.accent : V.text)
                metric("\(w.paused)", "paused", w.paused > 0 ? V.warning : V.text)
                metric("\(w.stopped)", "stopped", w.stopped > 0 ? V.danger : V.text)
                metric("\(w.refused_total)", "refused", w.refused_total > 0 ? V.warning : V.text)
                Spacer()
            }
            ForEach(w.workers) { row in
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Circle()
                        .fill(color(row.state))
                        .frame(width: 5, height: 5)
                    Text(row.worker).font(V.Font.label).foregroundColor(V.text)
                    Text(row.state).font(V.Font.caption).foregroundColor(color(row.state))
                    if !row.last_tool.isEmpty {
                        Text(row.last_tool).font(V.Font.mono).foregroundColor(V.textTertiary)
                            .lineLimit(1)
                    }
                    Spacer()
                    if row.in_flight > 0 {
                        Text("\(row.in_flight) in flight").font(V.Font.caption)
                            .foregroundColor(V.accent)
                    }
                    if row.calls_refused > 0 {
                        Text("\(row.calls_refused) refused").font(V.Font.caption)
                            .foregroundColor(V.warning)
                    }
                }
                .padding(.vertical, 1)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    @ViewBuilder
    private func metric(_ value: String, _ label: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            Text(value).font(V.Font.label).foregroundColor(color)
            Text(label).font(V.Font.caption).foregroundColor(V.textTertiary)
        }
    }

    private func symbol(_ status: String) -> String {
        switch status {
        case "GRANTED", "NOT_REQUIRED": return "checkmark.circle.fill"
        case "DENIED", "REQUIRES_MANUAL_SYSTEM_SETTINGS": return "xmark.circle.fill"
        case "NOT_DETERMINED": return "questionmark.circle"
        default: return "questionmark.circle"
        }
    }

    private func color(_ status: String) -> Color {
        switch status {
        case "GRANTED", "NOT_REQUIRED": return V.success
        case "DENIED", "REQUIRES_MANUAL_SYSTEM_SETTINGS": return V.danger
        case "NOT_DETERMINED": return V.warning
        case "running": return V.accent
        case "paused": return V.warning
        case "stopped": return V.danger
        default: return V.textTertiary
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            hud = try await api.get("/reality", as: APIModels.RealityHUD.self)
        } catch {
            errorMessage = "The machine could not be read: \(error.localizedDescription)"
        }
        loading = false
    }

    private func takeOver() async {
        working = true
        released = nil
        do {
            taken = try await api.post("/reality/takeover",
                                       body: TakeoverBody(reason: ""),
                                       as: APIModels.TakeoverResult.self)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }

    private func release() async {
        working = true
        taken = nil
        do {
            released = try await api.post("/reality/release",
                                          body: Optional<TakeoverBody>.none,
                                          as: APIModels.ReleaseResult.self)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }
}

private struct TakeoverBody: Codable {
    let reason: String
}