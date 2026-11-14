import SwiftUI

@available(iOS 16.0, *)
struct SettingsView: View {
    @EnvironmentObject private var session: SessionManager
    @EnvironmentObject private var backend: BackendManager
    @StateObject private var client = CompanionAPIClient.shared
    @State private var showingSignOut = false
    @State private var showingEmergencyStop = false

    var body: some View {
        NavigationStack {
            Form {
                connectionSection
                deviceSection
                actionsSection
                aboutSection
                dangerSection
            }
            .navigationTitle("Settings")
            .confirmationDialog("Sign Out?", isPresented: $showingSignOut) {
                Button("Sign Out", role: .destructive) { signOut() }
                Button("Cancel", role: .cancel) {}
            } message: { Text("Your session will be revoked. You'll need to re-pair to reconnect.") }
            .confirmationDialog("Emergency Stop?", isPresented: $showingEmergencyStop) {
                Button("Stop All", role: .destructive) { emergencyStop() }
                Button("Cancel", role: .cancel) {}
            } message: { Text("This halts all running workflows immediately. It cannot be undone.") }
        }
    }

    private var connectionSection: some View {
        Section("Connection") {
            HStack {
                Text("Backend")
                Spacer()
                Text(session.backendURL).foregroundStyle(.secondary).lineLimit(1)
            }
            HStack {
                Text("Status")
                Spacer()
                Circle().fill(backend.state.isConnected ? Color.green : Color.red).frame(width: 8, height: 8)
                Text(backend.state.label).foregroundStyle(.secondary)
            }
            if let health = backend.lastHealth {
                HStack {
                    Text("Surface")
                    Spacer()
                    Text(session.surface).foregroundStyle(.secondary)
                }
                HStack {
                    Text("LLM")
                    Spacer()
                    Text(health.ai_provider ?? "None").foregroundStyle(.secondary)
                }
            }
        }
    }

    private var deviceSection: some View {
        Section("Device Identity") {
            HStack {
                Text("Device ID")
                Spacer()
                Text(session.deviceId.isEmpty ? "(not set)" : session.deviceId).foregroundStyle(.secondary).font(.caption).lineLimit(1)
            }
            HStack {
                Text("Device Name")
                Spacer()
                Text(session.deviceName.isEmpty ? "(not set)" : session.deviceName).foregroundStyle(.secondary)
            }
            if !session.pairingId.isEmpty {
                HStack {
                    Text("Pairing ID")
                    Spacer()
                    Text(session.pairingId).foregroundStyle(.secondary).font(.caption).lineLimit(1)
                }
            }
        }
    }

    private var actionsSection: some View {
        Section("Actions") {
            Button(role: .destructive) { showingSignOut = true } label: {
                Label("Sign Out", systemImage: "rectangle.portrait.and.arrow.right")
            }
            Button(role: .destructive) { showingEmergencyStop = true } label: {
                Label("Emergency Stop", systemImage: "exclamationmark.octagon.fill")
            }
        }
    }

    private var aboutSection: some View {
        Section("About") {
            HStack {
                Text("Version")
                Spacer()
                Text("1.0 (companion)").foregroundStyle(.secondary)
            }
            if let health = backend.lastHealth {
                HStack {
                    Text("Server Version")
                    Spacer()
                    Text(health.system?.hostname ?? "unknown").foregroundStyle(.secondary)
                }
                HStack {
                    Text("Platform")
                    Spacer()
                    Text(health.system?.platform ?? "unknown").foregroundStyle(.secondary)
                }
            }
        }
    }

    private var dangerSection: some View {
        Section {
            Button(role: .destructive) {
                session.signOut()
                backend.disconnect()
            } label: {
                Label("Forget This Device", systemImage: "trash")
            }
        } header: {
            Text("Danger Zone")
        }
    }

    private func signOut() {
        Task {
            try? await client.signOut()
        }
        session.signOut()
        backend.disconnect()
    }

    private func emergencyStop() {
        Task {
            do {
                _ = try await client.emergencyStop()
            } catch {
                // Silently handled — the stop is fire-and-forget
            }
        }
    }
}