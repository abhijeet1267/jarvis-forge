import SwiftUI

/// Native command input and response interface.
///
/// Submits commands through the existing POST /command API endpoint
/// using the phone's authenticated session. Displays real execution
/// results, verification status, and confirmation requirements.
@available(iOS 16.0, *)
struct CommandView: View {

    @EnvironmentObject private var session: SessionManager
    @StateObject private var client = CompanionAPIClient.shared

    @State private var inputText: String = ""
    @State private var history: [CommandEntry] = []
    @State private var isSending: Bool = false
    @State private var errorMessage: String?
    @FocusState private var isFocused: Bool

    /// Quick action shortcuts — fill the input, execute the same send()
    private let quickActions: [(String, String)] = [
        ("📋", "Show my pending approvals"),
        ("🔍", "Find my latest project report"),
        ("📊", "Summarize my recent tasks"),
        ("🔌", "Check the connection to my devices"),
        ("🧠", "What can you help me with?"),
    ]

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // History
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            ForEach(history) { entry in
                                CommandBubble(entry: entry)
                            }

                            if let error = errorMessage {
                                Text(error)
                                    .font(.callout)
                                    .foregroundStyle(.red)
                                    .padding()
                            }

                            // Scroll anchor
                            Color.clear.frame(height: 1).id("bottom")
                        }
                        .padding()
                    }
                    .onChange(of: history.count) { _ in
                        withAnimation { proxy.scrollTo("bottom") }
                    }
                }

                // Quick actions
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(quickActions, id: \.1) { (icon, label) in
                            Button {
                                inputText = label
                                isFocused = true
                            } label: {
                                HStack(spacing: 4) {
                                    Text(icon)
                                    Text(label)
                                        .font(.caption2)
                                        .lineLimit(1)
                                }
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .background(.gray.opacity(0.1))
                                .cornerRadius(16)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal)
                    .padding(.bottom, 8)
                }

                // Input bar
                HStack(spacing: 8) {
                    TextField("Ask Vantage…", text: $inputText, axis: .vertical)
                        .textFieldStyle(.plain)
                        .padding(10)
                        .background(.gray.opacity(0.15))
                        .cornerRadius(20)
                        .focused($isFocused)
                        .onSubmit { send() }
                        .lineLimit(1...5)

                    Button(action: send) {
                        if isSending {
                            ProgressView()
                                .tint(.white)
                                .padding(8)
                        } else {
                            Image(systemName: "arrow.up")
                                .fontWeight(.semibold)
                                .padding(8)
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .clipShape(Circle())
                    .disabled(inputText.trimmingCharacters(in: .whitespaces).isEmpty || isSending)
                }
                .padding(.horizontal)
                .padding(.vertical, 8)
            }
            .navigationTitle("Command")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    if !history.isEmpty {
                        Button("Clear") { history.removeAll() }
                    }
                }
            }
        }
    }

    private func send() {
        let text = inputText.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty, !isSending else { return }

        let requestEntry = CommandEntry(text: text, role: .user)
        history.append(requestEntry)
        inputText = ""
        isSending = true
        errorMessage = nil

        Task {
            do {
                let cmd = CommandRequest(text: text)
                let response = try await client.sendCommand(cmd)

                let resultText = response.result
                    ?? response.error
                    ?? response.status
                    ?? "(no response)"

                let responseEntry = CommandEntry(
                    text: resultText,
                    role: .assistant,
                    status: response.status,
                    verified: response.verification?.status == "verified",
                    confirmationRequired: response.confirmation_required == true
                )
                history.append(responseEntry)
            } catch {
                let msg = (error as? CompanionClientError)?.errorDescription ?? error.localizedDescription
                let errorEntry = CommandEntry(text: "Error: \(msg)", role: .error)
                history.append(errorEntry)
                errorMessage = msg
            }
            isSending = false
        }
    }
}

// MARK: - Command Entry

struct CommandEntry: Identifiable {
    let id = UUID()
    let text: String
    let role: CommandRole
    let status: String?
    let verified: Bool
    let confirmationRequired: Bool
    let timestamp: Date

    init(
        text: String,
        role: CommandRole,
        status: String? = nil,
        verified: Bool = false,
        confirmationRequired: Bool = false,
        timestamp: Date = Date()
    ) {
        self.text = text
        self.role = role
        self.status = status
        self.verified = verified
        self.confirmationRequired = confirmationRequired
        self.timestamp = timestamp
    }
}

enum CommandRole {
    case user
    case assistant
    case error
}

// MARK: - Command Bubble

@available(iOS 16.0, *)
struct CommandBubble: View {
    let entry: CommandEntry

    var body: some View {
        HStack {
            if entry.role == .user || entry.role == .error { Spacer() }

            VStack(alignment: entry.role == .assistant ? .leading : .trailing, spacing: 6) {
                Text(entry.text)
                    .font(.callout)
                    .padding(12)
                    .background(bubbleColor)
                    .foregroundColor(entry.role == .assistant ? .primary : .white)
                    .cornerRadius(16)

                // Status indicators
                if entry.role == .assistant {
                    HStack(spacing: 8) {
                        if entry.confirmationRequired {
                            Label("Awaiting approval", systemImage: "clock.badge.questionmark")
                                .font(.caption2)
                                .foregroundStyle(.orange)
                        }
                        if entry.verified {
                            Label("Verified", systemImage: "checkmark.seal")
                                .font(.caption2)
                                .foregroundStyle(.green)
                        }
                        if let status = entry.status {
                            Label(status.capitalized, systemImage: "info.circle")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }

            if entry.role == .assistant { Spacer() }
        }
    }

    private var bubbleColor: Color {
        switch entry.role {
        case .user: return .blue
        case .assistant: return .gray.opacity(0.1)
        case .error: return .red.opacity(0.15)
        }
    }
}