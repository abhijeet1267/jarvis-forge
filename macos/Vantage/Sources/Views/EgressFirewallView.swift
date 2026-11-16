import SwiftUI

/// Trust & Privacy: what this Mac is allowed to contact, and the one switch that
/// changes the answer for the paths that carry the conversation.
///
/// The screen's job is to make an egress rule visible rather than implied. It
/// lists every declared destination with the reason it exists and the kind that
/// decides whether privacy mode blocks it, and it carries a live check: type an
/// address, see whether the firewall would let it out *right now* and why.
///
/// Two rules it keeps honestly:
///
/// * **It is not a network firewall.** The gate sits inside this process, on the
///   one read path everything uses. Traffic from other apps on the Mac is
///   outside it, and the limit note from the server is shown verbatim rather
///   than buried.
/// * **Turning privacy mode on is not the same as a silent lockout.** The paths
///   it blocks are named (providers carry the conversation), and lookups and
///   loopback keep working, so the switch has a visible, explainable effect.
struct EgressFirewallView: View {
    @State private var posture: APIModels.EgressPosture?
    @State private var probeURL = "https://example.com/anything"
    @State private var verdict: APIModels.EgressVerdict?
    @State private var loading = false
    @State private var checking = false
    @State private var toggling = false
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Egress Firewall",
                    subtitle: "Every address this Mac is allowed to reach, and why."
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
                    postureCard(posture)
                    checkCard(posture)
                    destinationList(posture)
                    limitCard(posture.not_covered)
                } else if !loading {
                    Text("The firewall posture could not be read.")
                        .font(V.Font.body).foregroundColor(V.textSecondary)
                }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - Posture

    @ViewBuilder
    private func postureCard(_ p: APIModels.EgressPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            HStack(spacing: V.Space.s) {
                Image(systemName: p.privacy_mode ? "lock.shield.fill" : "lock.shield")
                    .font(.system(size: 13))
                    .foregroundColor(p.privacy_mode ? V.success : V.textTertiary)
                Text("Privacy mode \(p.privacy_mode ? "on" : "off")")
                    .font(V.Font.cardTitle).foregroundColor(V.text)
                Spacer()
                Text("\(p.declared_hosts) declared hosts")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                Button(toggling ? "…" : (p.privacy_mode ? "Turn off" : "Turn on")) {
                    Task { await setPrivacy(!p.privacy_mode) }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(toggling)
            }

            Text(p.privacy_mode
                 ? "Provider endpoints are refused; lookups and this Mac keep working. "
                 + "A model call will report a refusal, not a network error."
                 : "Model endpoints are reachable. Turning this on refuses them, so "
                 + "conversation content stays on this Mac.")
                .font(V.Font.body).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: V.Space.l) {
                ForEach(["loopback", "lookup", "service", "provider"], id: \.self) { kind in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(p.by_kind[kind] ?? 0)")
                            .font(V.Font.cardTitle).foregroundColor(color(for: kind))
                        Text(kind).font(V.Font.caption).foregroundColor(V.textTertiary)
                    }
                }
                Spacer()
                Text("enforced at \(p.enforced_at)")
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Live check

    @ViewBuilder
    private func checkCard(_ p: APIModels.EgressPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            Text("CHECK AN ADDRESS").font(V.Font.caption).foregroundColor(V.textTertiary)

            HStack(spacing: V.Space.s) {
                TextField("https://…", text: $probeURL)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.mono)
                    .onSubmit { Task { await check() } }
                Button(checking ? "Checking…" : "Check") { Task { await check() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(checking || probeURL.trimmingCharacters(in: .whitespaces).isEmpty)
            }

            if let v = verdict {
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: v.allowed ? "checkmark.shield.fill" : "xmark.shield.fill")
                        .font(.system(size: 11))
                        .foregroundColor(v.allowed ? V.success : V.danger)
                    Text(v.decision.uppercased())
                        .font(V.Font.label)
                        .foregroundColor(v.allowed ? V.success : V.danger)
                    Text(v.host).font(V.Font.mono).foregroundColor(V.textSecondary)
                    Text("· \(v.kind)").font(V.Font.caption).foregroundColor(V.textTertiary)
                    if v.privacy_mode {
                        Text("· privacy on").font(V.Font.caption).foregroundColor(V.warning)
                    }
                    Spacer()
                }
                Text(v.reason)
                    .font(V.Font.caption).foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("The check asks the firewall without making the request, so it "
                     + "cannot send anything on its own.")
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Destinations

    @ViewBuilder
    private func destinationList(_ p: APIModels.EgressPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("DECLARED DESTINATIONS").font(V.Font.caption).foregroundColor(V.textTertiary)

            ForEach(p.destinations) { dest in
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: symbol(for: dest.kind))
                        .font(.system(size: 10))
                        .foregroundColor(color(for: dest.kind))
                        .frame(width: 14)
                    Text(dest.host).font(V.Font.mono).foregroundColor(V.text)
                    Text(dest.kind).font(V.Font.caption).foregroundColor(V.textTertiary)
                        .frame(width: 62, alignment: .leading)
                    Text(dest.why)
                        .font(V.Font.caption).foregroundColor(V.textSecondary)
                        .lineLimit(1)
                    Spacer()
                    if dest.private_ok, p.privacy_mode == false {
                        Text("private ok")
                            .font(V.Font.caption).foregroundColor(V.textTertiary)
                    } else if p.privacy_mode, dest.kind != "loopback", dest.kind != "lookup" {
                        Text("blocked")
                            .font(V.Font.caption).foregroundColor(V.danger)
                    }
                }
                .padding(.vertical, 3)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - The honest limit

    @ViewBuilder
    private func limitCard(_ note: String) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            Text("WHAT THIS DOES NOT COVER")
                .font(V.Font.caption).foregroundColor(V.textTertiary)
            Text(note)
                .font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Style

    private func color(for kind: String) -> Color {
        switch kind {
        case "loopback": return V.textSecondary
        case "lookup": return V.accent
        case "provider": return V.warning
        case "service": return V.text
        default: return V.textTertiary
        }
    }

    private func symbol(for kind: String) -> String {
        switch kind {
        case "loopback": return "house"
        case "lookup": return "magnifyingglass"
        case "provider": return "brain"
        case "service": return "cloud"
        default: return "questionmark"
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            posture = try await api.get("/egress", as: APIModels.EgressPosture.self)
            // Answer the question the screen exists for on open: would a model
            // call get out right now? It is read-only, so asking unasked costs
            // nothing and saves a click into a panel to find the answer out.
            if verdict == nil { await check() }
        } catch {
            errorMessage = error.localizedDescription
        }
        loading = false
    }

    private func check() async {
        let url = probeURL.trimmingCharacters(in: .whitespaces)
        guard !url.isEmpty else { return }
        checking = true
        errorMessage = nil
        do {
            verdict = try await api.post("/egress/check",
                                         body: APIModels.EgressCheckRequest(url: url),
                                         as: APIModels.EgressVerdict.self)
        } catch {
            verdict = nil
            errorMessage = error.localizedDescription
        }
        checking = false
    }

    private func setPrivacy(_ enabled: Bool) async {
        toggling = true
        errorMessage = nil
        do {
            // Reuses the existing switch rather than a second one: privacy mode is
            // one setting, and a second route to it would be a second source of truth.
            _ = try await api.post("/privacy/mode?enabled=\(enabled)",
                                   body: Optional<EmptyBody>.none,
                                   as: PrivacyModeResult.self)
            await load()
            verdict = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        toggling = false
    }
}

private struct EmptyBody: Codable {}

/// `POST /privacy/mode` replies with the new value of the one setting.
private struct PrivacyModeResult: Codable {
    let privacy_mode: Bool
}