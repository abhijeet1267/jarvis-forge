import SwiftUI

/// Initial setup and pairing flow.
///
/// Flow:
/// 1. User enters the Mac's IP address (or discovers it via Bonjour)
/// 2. App fetches the phone page, extracts the bootstrap code
/// 3. App exchanges bootstrap for a session token
/// 4. On success, transitions to the main app
@available(iOS 16.0, *)
struct SetupView: View {

    @EnvironmentObject private var session: SessionManager
    @EnvironmentObject private var backend: BackendManager
    @StateObject private var client = CompanionAPIClient.shared

    @State private var hostInput: String = ""
    @State private var status: SetupStatus = .idle
    @State private var errorMessage: String?

    private enum SetupStatus: Equatable {
        case idle
        case fetching
        case exchanging
        case connecting
        case done
        case error(String)
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                Spacer()

                // Header
                VStack(spacing: 8) {
                    Image(systemName: "shield.lefthalf.filled")
                        .font(.system(size: 60))
                        .foregroundStyle(LinearGradient(
                            colors: [.blue, .purple],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        ))

                    Text("Vantage Companion")
                        .font(.largeTitle)
                        .fontWeight(.bold)

                    Text("Connect to your Mac to control Vantage from your phone.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 32)
                }

                // Host input
                VStack(alignment: .leading, spacing: 8) {
                    Text("Mac Address")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.leading, 4)

                    HStack {
                        TextField("192.168.1.100:8765", text: $hostInput)
                            .textFieldStyle(.plain)
#if os(iOS)
                            .keyboardType(.URL)
                            .autocapitalization(.none)
                            .disableAutocorrection(true)
#endif
                            .padding(12)
                            .background(.ultraThickMaterial)
                            .cornerRadius(10)
                    }
                }
                .padding(.horizontal, 32)

                // Discovered hosts
                if !backend.discoveredHosts.isEmpty && status == .idle {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Discovered on network")
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        ForEach(backend.discoveredHosts, id: \.self) { host in
                            Button {
                                hostInput = host
                            } label: {
                                HStack {
                                    Image(systemName: "wifi")
                                        .font(.caption)
                                    Text(host)
                                        .font(.callout)
                                    Spacer()
                                    if hostInput == host {
                                        Image(systemName: "checkmark")
                                            .foregroundColor(.blue)
                                    }
                                }
                                .padding(10)
                                .background(.gray.opacity(0.15))
                                .cornerRadius(8)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 32)
                }

                // Status
                if status != .idle && status != .done {
                    HStack(spacing: 8) {
                        if status != .error("") {
                            ProgressView()
                        }
                        Text(statusText)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.top, 8)
                }

                // Error
                if let error = errorMessage {
                    Text(error)
                        .font(.callout)
                        .foregroundStyle(.red)
                        .padding(.horizontal, 32)
                        .multilineTextAlignment(.center)
                }

                // Connect button
                Button(action: connect) {
                    HStack {
                        if status == .fetching || status == .exchanging || status == .connecting {
                            ProgressView()
                                .tint(.white)
                        }
                        Text(buttonLabel)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .background(hostInput.trimmingCharacters(in: .whitespaces).isEmpty
                                ? Color.blue.opacity(0.4) : Color.blue)
                    .foregroundColor(.white)
                    .cornerRadius(12)
                    .font(.headline)
                }
                .disabled(hostInput.trimmingCharacters(in: .whitespaces).isEmpty
                          || status == .fetching || status == .exchanging || status == .connecting)
                .padding(.horizontal, 32)

                // Discover button
                Button {
                    backend.startDiscovery()
                } label: {
                    Label("Discover on Network", systemImage: "dot.radiowaves.left.and.right")
                        .font(.callout)
                }
                .padding(.top, 4)

                Spacer()

                // Footer
                Text("Your phone must be on the same local network as your Mac.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .padding(.bottom, 24)
            }
            .navigationTitle("Setup")
            .navigationBarTitleDisplayMode(.inline)
            .onAppear {
                // Pre-fill with last used backend URL
                if hostInput.isEmpty {
                    hostInput = session.backendURL
                }
            }
        }
    }

    private var statusText: String {
        switch status {
        case .fetching: return "Fetching pairing code…"
        case .exchanging: return "Authenticating…"
        case .connecting: return "Verifying connection…"
        default: return ""
        }
    }

    private var buttonLabel: String {
        switch status {
        case .fetching: return "Fetching…"
        case .exchanging: return "Authenticating…"
        case .connecting: return "Connecting…"
        default: return "Connect"
        }
    }

    private func connect() {
        let host = hostInput.trimmingCharacters(in: .whitespaces)
        guard !host.isEmpty else { return }

        errorMessage = nil
        status = .fetching
        session.saveBackendURL(host)

        Task {
            do {
                // Step 1: Fetch bootstrap from phone page
                status = .fetching
                let bootstrap = try await client.fetchBootstrapFromPhonePage()

                guard !bootstrap.isEmpty else {
                    throw CompanionClientError.transport("No bootstrap code found. Is the server running?")
                }

                // Step 2: Exchange bootstrap for session token
                status = .exchanging
                let sessionResp = try await client.exchangeBootstrap(code: bootstrap)

                guard sessionResp.session_token != nil else {
                    throw CompanionClientError.unauthorized
                }

                // Step 3: Verify connection with health check
                status = .connecting
                await backend.connect(to: host)

                // Step 4: Save device identity
                let deviceId = "phone_\(UUID().uuidString.prefix(8))"
                let deviceName: String = {
#if os(iOS)
                    UIDevice.current.name
#else
                    Host.current().name ?? "Vantage Companion"
#endif
                }()
                session.saveDeviceIdentity(id: deviceId, name: deviceName)
                session.saveBackendURL(host)

                status = .done
            } catch {
                let msg = (error as? CompanionClientError)?.errorDescription ?? error.localizedDescription
                errorMessage = msg
                status = .error(msg)
            }
        }
    }
}