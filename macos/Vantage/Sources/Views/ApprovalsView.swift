import SwiftUI

/// Pending confirmations requiring user approval.
///
/// The screen has one job: let a person see exactly what will happen and answer
/// it. A card is gold while it is pending, carries the preview the backend wrote,
/// and disables both buttons while a decision is in flight so a double-click
/// cannot send two answers. A failed list read is never drawn as "nothing
/// pending" — that would be a claim the screen did not check.
struct ApprovalsView: View {

    @State private var confirmations: [APIModels.Confirmation] = []
    @State private var isLoading = true
    /// A refresh after the first load: the list stays readable while it works.
    @State private var isRefreshing = false
    @State private var errorMessage: String?
    @State private var lastLoaded: Date?
    @State private var banner: String?
    /// A card whose decision is being sent. One at a time: the answer is a
    /// mutating call, and a person should not be able to answer two cards from
    /// one gesture.
    @State private var deciding: String?

    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header

            if let error = errorMessage {
                Notice(kind: .error, message: error, actionTitle: "Retry") {
                    Task { await load() }
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.bottom, V.Space.s)
            }

            if let banner {
                Notice(kind: .success, message: banner, actionTitle: "Dismiss") {
                    self.banner = nil
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.bottom, V.Space.s)
            }

            if isLoading {
                Spacer()
                LoadingRow(text: "Checking for approvals…")
                    .frame(maxWidth: .infinity)
                Spacer()
            } else if confirmations.isEmpty {
                Spacer()
                if errorMessage != nil {
                    // A failed read is NOT an empty list. Both used to render at
                    // the same time: "Could not connect to the server" above, and
                    // "Nothing requires approval right now" below. The second is a
                    // claim about the backend, and this screen had not read it.
                    EmptyState(
                        symbol: "questionmark.circle",
                        title: "Could not check",
                        message: "I could not check for approvals, so this is not an empty list.",
                        actionTitle: "Try again",
                        action: { Task { await load() } })
                } else {
                    EmptyState(
                        symbol: "checkmark.shield",
                        title: "Nothing waiting",
                        message: "Nothing requires approval right now.")
                }
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: V.Space.m) {
                        ForEach(confirmations) { confirmation in
                            ConfirmationCard(
                                confirmation: confirmation,
                                isDeciding: deciding == confirmation.id,
                                decisionsLocked: deciding != nil,
                                onApprove: { await handle(confirmation.id, approve: true) },
                                onDeny: { await handle(confirmation.id, approve: false) }
                            )
                        }
                    }
                    .padding(.horizontal, V.Space.gutter)
                    .padding(.bottom, V.Space.section)
                    .frame(maxWidth: 700, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .center)
                }
            }
        }
        .background(V.background)
        .navigationTitle("Approvals")
        .task { await load() }
        .refreshable { await load() }
    }

    private var header: some View {
        PageHeader(
            title: "Approvals",
            subtitle: "Consequential actions wait here until you answer."
        ) {
            HStack(spacing: V.Space.s) {
                if !confirmations.isEmpty {
                    StatusPill(kind: .pending, text: "\(confirmations.count) pending")
                }
                if isRefreshing { ProgressView().controlSize(.small) }
                if let stamp = lastLoaded, !isRefreshing {
                    Text("Updated \(relative(stamp))")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                }
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(isLoading || isRefreshing || deciding != nil)
            }
        }
        .padding(.top, V.Space.section)
        .padding(.bottom, V.Space.l)
        .frame(maxWidth: 700, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .center)
    }

    private func relative(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private func load() async {
        if confirmations.isEmpty {
            isLoading = true
        } else {
            isRefreshing = true
        }
        errorMessage = nil
        do {
            confirmations = try await api.pendingConfirmations()
            lastLoaded = Date()
        } catch {
            // A stale list must not survive a failed read: those cards are claims
            // this load could not confirm. Clearing is what makes the empty
            // branch above say "could not check" rather than "nothing waiting".
            errorMessage = error.localizedDescription
            confirmations = []
        }
        isLoading = false
        isRefreshing = false
    }

    private func handle(_ confirmationId: String, approve: Bool) async {
        guard deciding == nil else { return }
        deciding = confirmationId
        defer { deciding = nil }
        do {
            // The approver's session is sent, because the backend records *who*
            // answered: a confirmation answered on the phone and one answered on
            // the laptop are not the same fact, and the audit needs to tell them
            // apart.
            let response = try await api.resolveConfirmation(
                confirmationId, approve: approve, sessionId: Self.approverSession)
            banner = approve ? "Approved." : "Denied."
            // The reply is spoken after approval — a server message, never an
            // invented confirmation.
            if let reply = response.reply, !reply.isEmpty, approve {
                VoiceService.shared.speak(reply)
            }
        } catch {
            // A failed decision changes nothing: the request is still pending,
            // and saying otherwise would be a lie.
            errorMessage = error.localizedDescription
        }
        await load()
    }

    /// The session this app approves as. Recorded by the backend so an approval
    /// can be attributed to a surface.
    private static let approverSession = "native"
}

// MARK: - Confirmation card

struct ConfirmationCard: View {
    let confirmation: APIModels.Confirmation
    var isDeciding = false
    var decisionsLocked = false
    var onApprove: () async -> Void
    var onDeny: () async -> Void

    private var isPending: Bool {
        // The list is `/pending*`, but a payload that carries a resolved state
        // must not be drawn as still waiting.
        (confirmation.state ?? "pending") == "pending"
    }

    private var stateKind: StatusKind {
        switch confirmation.state {
        case "approved": return .connected
        case "denied": return .idle
        case "executing": return .pending
        case "failed": return .error
        default: return .pending
        }
    }

    private var stateLabel: String {
        (confirmation.state ?? "pending").capitalized
    }

    var body: some View {
        Card(tone: isPending ? .gold : .plain) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: "exclamationmark.shield.fill")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(isPending ? V.gold : stateKind.colour)
                    Text(confirmation.displayTitle)
                        .font(V.Font.cardTitle)
                        .foregroundStyle(V.text)
                    Spacer(minLength: V.Space.s)
                    StatusPill(kind: stateKind, text: stateLabel)
                    if let risk = confirmation.risk, risk > 0 {
                        Text("risk \(risk)")
                            .font(V.Font.caption)
                            .foregroundStyle(V.textTertiary)
                    }
                }

                if !confirmation.displayDetail.isEmpty {
                    Text(confirmation.displayDetail)
                        .font(V.Font.message)
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    if let created = confirmation.created_at {
                        Text(created)
                            .font(V.Font.caption)
                            .foregroundStyle(V.textTertiary)
                    }
                    if let device = confirmation.device {
                        Text("·")
                            .font(V.Font.caption)
                            .foregroundStyle(V.textTertiary)
                        Text(device)
                            .font(V.Font.caption)
                            .foregroundStyle(V.textTertiary)
                    }
                    Spacer()

                    if isDeciding {
                        ProgressView().controlSize(.small)
                        Text("Sending…")
                            .font(V.Font.caption)
                            .foregroundStyle(V.textSecondary)
                    }

                    if isPending {
                        Button("Deny") {
                            Task { await onDeny() }
                        }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(isDeciding || decisionsLocked)

                        Button("Approve") {
                            Task { await onApprove() }
                        }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(isDeciding || decisionsLocked)
                    }
                }
            }
        }
    }
}