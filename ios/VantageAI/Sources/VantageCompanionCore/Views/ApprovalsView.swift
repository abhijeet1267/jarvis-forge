import SwiftUI

/// Pending confirmations for which this phone session is authorized to respond.
///
/// Displays each confirmation with action details, parameters, risk level,
/// and approve/deny buttons. Approval and denial use the existing
/// confirmation lifecycle (POST /confirm/{id}/approve and /confirm/{id}/deny).
@available(iOS 16.0, *)
struct ApprovalsView: View {

    @StateObject private var client = CompanionAPIClient.shared
    @State private var confirmations: [Confirmation] = []
    @State private var isLoading: Bool = false
    @State private var errorMessage: String?
    @State private var processingIds: Set<String> = []

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    ProgressView("Loading confirmations…")
                } else if confirmations.isEmpty {
                    emptyState
                } else {
                    confirmationList
                }
            }
            .navigationTitle("Approvals")
            .refreshable { await load() }
            .task { await load() }
        }
    }

    // MARK: - Empty State

    private var emptyState: some View {
        VStack(spacing: 16) {
            Image(systemName: "checkmark.shield")
                .font(.system(size: 48))
                .foregroundStyle(.secondary)

            Text("No Pending Approvals")
                .font(.title3)
                .fontWeight(.semibold)

            Text("When actions require your approval, they will appear here.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
    }

    // MARK: - List

    private var confirmationList: some View {
        List {
            if let error = errorMessage {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(.red)
            }

            ForEach(confirmations) { confirmation in
                ConfirmationRow(
                    confirmation: confirmation,
                    isProcessing: processingIds.contains(confirmation.id),
                    onApprove: { await approve(confirmation) },
                    onDeny: { await deny(confirmation) }
                )
            }
        }
        .listStyle(.plain)
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            confirmations = try await client.fetchConfirmations()
        } catch {
            let msg = (error as? CompanionClientError)?.errorDescription ?? error.localizedDescription
            errorMessage = msg
        }
        isLoading = false
    }

    private func approve(_ confirmation: Confirmation) async {
        processingIds.insert(confirmation.id)
        do {
            _ = try await client.approveConfirmation(confirmation.id)
            confirmations.removeAll { $0.id == confirmation.id }
        } catch {
            errorMessage = (error as? CompanionClientError)?.errorDescription ?? error.localizedDescription
        }
        processingIds.remove(confirmation.id)
    }

    private func deny(_ confirmation: Confirmation) async {
        processingIds.insert(confirmation.id)
        do {
            _ = try await client.denyConfirmation(confirmation.id)
            confirmations.removeAll { $0.id == confirmation.id }
        } catch {
            errorMessage = (error as? CompanionClientError)?.errorDescription ?? error.localizedDescription
        }
        processingIds.remove(confirmation.id)
    }
}

// MARK: - Confirmation Row

@available(iOS 16.0, *)
private struct ConfirmationRow: View {

    let confirmation: Confirmation
    let isProcessing: Bool
    let onApprove: () async -> Void
    let onDeny: () async -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(confirmation.actionDescription)
                        .font(.headline)

                    Text(confirmation.targetDescription)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                // Risk badge
                Text(riskLabel)
                    .font(.caption2)
                    .fontWeight(.medium)
                    .foregroundColor(riskColor)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(riskColor.opacity(0.15))
                    .cornerRadius(6)
            }

            // Parameters (if present)
            if let params = confirmation.parameters, !params.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(params.keys.sorted()), id: \.self) { key in
                        HStack {
                            Text(key)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(String(describing: params[key] ?? .null))
                                .font(.caption)
                                .lineLimit(1)
                        }
                    }
                }
                .padding(8)
                .background(.gray.opacity(0.15))
                .cornerRadius(8)
            }

            // Details from detail dict
            if let details = confirmation.detail, !details.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(details.keys.sorted()), id: \.self) { key in
                        HStack {
                            Text(key)
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                            Text(String(describing: details[key] ?? .null))
                                .font(.caption2)
                                .lineLimit(1)
                        }
                    }
                }
            }

            // Timestamps
            if let created = confirmation.created_at {
                Text("Requested \(formattedDate(created))")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            if let expires = confirmation.expires_at {
                Text("Expires \(formattedDate(expires))")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }

            // Actions
            HStack(spacing: 12) {
                if isProcessing {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                } else {
                    Button {
                        Task { await onApprove() }
                    } label: {
                        Label("Approve", systemImage: "checkmark")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.green)

                    Button {
                        Task { await onDeny() }
                    } label: {
                        Label("Deny", systemImage: "xmark")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .tint(.red)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private var riskLabel: String {
        switch confirmation.riskLevel {
        case "high": return "High Risk"
        case "medium": return "Medium"
        case "low": return "Low Risk"
        default: return confirmation.riskLevel.capitalized
        }
    }

    private var riskColor: Color {
        switch confirmation.riskLevel {
        case "high": return .red
        case "medium": return .orange
        case "low": return .green
        default: return .secondary
        }
    }

    private func formattedDate(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: iso) {
            return date.formatted(.relative(presentation: .named))
        }

        formatter.formatOptions = [.withInternetDateTime]
        if let date = formatter.date(from: iso) {
            return date.formatted(.relative(presentation: .named))
        }

        return iso
    }
}