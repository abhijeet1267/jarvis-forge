import SwiftUI

/// Safety Center: what Vantage may currently do, and how to stop it.
///
/// The screen leads with the question a person actually has -- "what am I
/// currently allowing?" -- and answers it with the live leases from the backend,
/// their exact scope, when they expire, and a revoke control per lease. Nothing
/// here is decorative: revoking writes through to the backend, and the list is
/// re-read afterwards so the screen cannot show a lease that is already gone.
struct SafetyCenterView: View {

    @State private var leases: [APIModels.PermissionLease] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var revoking: String?
    @State private var confirmStop = false
    @State private var delta: APIModels.TrustDelta?
    @State private var deltaError: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Safety Center",
                           subtitle: "Scoped authorizations Vantage holds right now.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await load() } })
                }

                emergencyStop
                trustDelta

                SectionHeader("Active leases")
                if leases.isEmpty && !isLoading && errorMessage == nil {
                    EmptyState(
                        symbol: "checkmark.shield",
                        title: "No active leases",
                        message: "Vantage holds no standing authorization. Each "
                            + "mission must earn its own, and they expire.")
                }

                ForEach(leases) { lease in
                    leaseRow(lease)
                }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
        .task { await load() }
        .alert("Stop all authorized work?", isPresented: $confirmStop) {
            Button("Cancel", role: .cancel) {}
            Button("Revoke every lease", role: .destructive) {
                Task { await revokeAll() }
            }
        } message: {
            Text("Every active lease will be revoked. Work that needed one will "
                 + "stop rather than continue on a stale authorization.")
        }
    }

    // MARK: - Emergency stop

    private var emergencyStop: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            SectionHeader("Emergency stop")
            Text("Revokes every active lease immediately. New work must earn fresh "
                 + "authorization; nothing continues on a revoked grant.")
                .font(V.Font.caption)
                .foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            Button("Revoke all leases") { confirmStop = true }
                .buttonStyle(VPrimaryButtonStyle())
                .tint(V.danger)
                .disabled(leases.isEmpty)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.dangerSoft)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Trust Delta

    /// Compare two authorization scopes, so a widened plan is visible here.
    ///
    /// The comparison is the backend's, not the view's: `scope_delta` is the one
    /// implementation of "what changed", and re-deriving it in Swift would let
    /// the two disagree about whether something needs re-approval.
    private var trustDelta: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            SectionHeader("Trust Delta")
            Text("Compare what a plan asks for now against what it asked before. "
                 + "Any widening needs fresh approval; a previous approval does not "
                 + "cover it.")
                .font(V.Font.caption)
                .foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: V.Space.s) {
                Button("Compare a widened plan") { Task { await compareDemo() } }
                    .buttonStyle(VPrimaryButtonStyle())
                Spacer()
            }

            if let deltaError {
                Text(deltaError).font(V.Font.caption).foregroundColor(V.danger)
            }

            if let delta {
                VStack(alignment: .leading, spacing: V.Space.xxs) {
                    if !delta.added_capabilities.isEmpty {
                        Text("New capabilities: \(delta.added_capabilities.joined(separator: ", "))")
                            .font(V.Font.mono).foregroundColor(V.warning)
                    }
                    if !delta.added_resource_prefixes.isEmpty {
                        Text("New resources: \(delta.added_resource_prefixes.joined(separator: ", "))")
                            .font(V.Font.mono).foregroundColor(V.warning)
                    }
                    if delta.increased_risk > 0 {
                        Text("Risk increased by \(delta.increased_risk)")
                            .font(V.Font.caption).foregroundColor(V.warning)
                    }
                    Text(delta.note)
                        .font(V.Font.caption)
                        .foregroundColor(delta.requires_new_approval ? V.warning : V.textSecondary)
                }
                .padding(V.Space.s)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(V.surface)
                .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
            }
        }
    }

    /// A comparison the person can actually see the behaviour of.
    private func compareDemo() async {
        deltaError = nil
        let before = APIModels.IntentScopePayload(
            mission_id: "preview", goal: "organize a project",
            allowed_capabilities: ["write_file"],
            allowed_resource_prefixes: ["/tmp/project"], risk_budget: 1)
        let after = APIModels.IntentScopePayload(
            mission_id: "preview", goal: "organize a project",
            allowed_capabilities: ["write_file", "delete_file"],
            allowed_resource_prefixes: ["/tmp/project", "/tmp/archive"],
            risk_budget: 2)
        do {
            delta = try await api.post(
                "/intelligence/intent/delta",
                body: APIModels.TrustDeltaRequest(old: before, new: after),
                as: APIModels.TrustDelta.self)
        } catch {
            deltaError = error.localizedDescription
        }
    }

    // MARK: - One lease

    private func leaseRow(_ lease: APIModels.PermissionLease) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(spacing: V.Space.s) {
                Text(lease.capability.isEmpty ? "any capability" : lease.capability)
                    .font(V.Font.cardTitle)
                    .foregroundColor(V.text)
                Spacer()
                Text("\(lease.remaining_uses) of \(lease.max_uses) uses left")
                    .font(V.Font.caption)
                    .foregroundColor(lease.remaining_uses > 0 ? V.textSecondary : V.danger)
            }

            Text("Scope: \(lease.scope.isEmpty ? "nothing" : lease.scope.joined(separator: ", "))")
                .font(V.Font.mono)
                .foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: V.Space.m) {
                Text("Expires \(expiryText(lease.expires_at))")
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                Text("Mission \(lease.mission_id)")
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                Spacer()
                Button(revoking == lease.lease_id ? "Revoking…" : "Revoke") {
                    Task { await revoke(lease.lease_id) }
                }
                .buttonStyle(.plain)
                .font(V.Font.label)
                .foregroundColor(V.danger)
                .disabled(revoking != nil)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
        .overlay(RoundedRectangle(cornerRadius: V.Radius.card)
            .stroke(V.border, lineWidth: 1))
    }

    private func expiryText(_ epoch: Double) -> String {
        guard epoch > 0 else { return "unknown" }
        let seconds = Int(epoch - Date().timeIntervalSince1970)
        if seconds <= 0 { return "expired" }
        if seconds < 60 { return "in \(seconds)s" }
        if seconds < 3600 { return "in \(seconds / 60)m" }
        return "in \(seconds / 3600)h"
    }

    // MARK: - Transport

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            let response: APIModels.LeaseList =
                try await api.get("/intelligence/leases/active")
            leases = response.leases
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func revoke(_ leaseID: String) async {
        revoking = leaseID
        do {
            _ = try await api.postEmpty("/intelligence/leases/\(leaseID)/revoke")
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        revoking = nil
    }

    private func revokeAll() async {
        revoking = ""
        do {
            _ = try await api.postEmpty("/intelligence/leases/revoke-all")
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        revoking = nil
    }
}