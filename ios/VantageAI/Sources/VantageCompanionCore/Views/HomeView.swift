import SwiftUI

/// Home dashboard showing connection status, model info, pending confirmations,
/// and recent activity.
@available(iOS 16.0, *)
struct HomeView: View {

    @EnvironmentObject private var session: SessionManager
    @EnvironmentObject private var backend: BackendManager
    @StateObject private var client = CompanionAPIClient.shared

    @State private var tasks: [TaskInfo] = []
    @State private var memoryCount: Int?
    @State private var tools: [ToolInfo] = []

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    connectionCard
                    modelCard
                    statsGrid
                    pendingCard
                    quickActions
                }
                .padding()
            }
            .navigationTitle("Vantage")
            .refreshable { await refresh() }
            .task { await refresh() }
        }
    }

    // MARK: - Connection Card

    private var connectionCard: some View {
        VStack(spacing: 8) {
            HStack {
                Circle()
                    .fill(backend.state.isConnected ? Color.green : Color.red)
                    .frame(width: 10, height: 10)

                Text(backend.state.isConnected ? "Connected" : backend.state.label)
                    .font(.headline)

                Spacer()

                Text(session.backendURL)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let health = backend.lastHealth {
                HStack(spacing: 16) {
                    Label("\(health.tools) tools", systemImage: "wrench")
                    if let provider = health.ai_provider {
                        Label(provider, systemImage: "brain")
                    }
                    Spacer()
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .padding()
        .background(.gray.opacity(0.15))
        .cornerRadius(12)
    }

    // MARK: - Model Status

    private var modelCard: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text("AI Model")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if let health = backend.lastHealth {
                    if health.llm_enabled {
                        Text(health.ai_provider ?? "Configured")
                            .font(.headline)
                            .foregroundColor(.green)
                    } else {
                        Text("Not configured")
                            .font(.headline)
                            .foregroundColor(.orange)
                    }
                } else {
                    Text("Unknown")
                        .font(.headline)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()

            Image(systemName: "brain.head.profile")
                .font(.title)
                .foregroundStyle(.purple)
        }
        .padding()
        .background(.gray.opacity(0.15))
        .cornerRadius(12)
    }

    // MARK: - Stats Grid

    private var statsGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
            StatCard(
                title: "Pending",
                value: "\(backend.lastHealth?.pending_confirmations ?? 0)",
                icon: "clock.badge.questionmark",
                color: .orange
            )

            StatCard(
                title: "Tasks",
                value: "\(tasks.count)",
                icon: "list.clipboard",
                color: .blue
            )

            StatCard(
                title: "Memory",
                value: memoryCount.map(String.init) ?? "—",
                icon: "brain",
                color: .purple
            )

            StatCard(
                title: "Tools",
                value: "\(tools.count)",
                icon: "hammer",
                color: .teal
            )
        }
    }

    // MARK: - Pending Card

    private var pendingCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: "checkmark.shield")
                    .foregroundColor(.orange)
                Text("Pending Confirmations")
                    .font(.headline)
                Spacer()
                Text("\(backend.lastHealth?.pending_confirmations ?? 0)")
                    .font(.title2)
                    .fontWeight(.bold)
                    .foregroundColor(.orange)
            }

            if (backend.lastHealth?.pending_confirmations ?? 0) > 0 {
                NavigationLink {
                    ApprovalsView()
                } label: {
                    Label("Review in Approvals tab", systemImage: "arrow.right")
                        .font(.callout)
                }
            }
        }
        .padding()
        .background(.gray.opacity(0.15))
        .cornerRadius(12)
    }

    // MARK: - Quick Actions

    private var quickActions: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Quick Actions")
                .font(.headline)
                .padding(.leading, 4)

            Button {
                Task { await refresh() }
            } label: {
                Label("Refresh Status", systemImage: "arrow.triangle.2.circlepath")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.bordered)
        }
    }

    // MARK: - Data

    private func refresh() async {
        async let tasksFetch: () = refreshTasks()
        async let memoryFetch: () = refreshMemory()
        async let toolsFetch: () = refreshTools()
        _ = await (tasksFetch, memoryFetch, toolsFetch)
    }

    private func refreshTasks() async {
        do {
            let response = try await client.fetchTasks()
            tasks = response.tasks ?? []
        } catch {
            // Silent failure — show cached data
        }
    }

    private func refreshMemory() async {
        do {
            let items = try await client.fetchMemory()
            memoryCount = items.count
        } catch {
            // Silent
        }
    }

    private func refreshTools() async {
        do {
            tools = try await client.fetchTools()
        } catch {
            // Silent
        }
    }
}

// MARK: - Stat Card

@available(iOS 16.0, *)
private struct StatCard: View {
    let title: String
    let value: String
    let icon: String
    let color: Color

    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: icon)
                .font(.title3)
                .foregroundStyle(color)

            Text(value)
                .font(.title2)
                .fontWeight(.bold)

            Text(title)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 14)
        .background(.gray.opacity(0.15))
        .cornerRadius(12)
    }
}