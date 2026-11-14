import SwiftUI

/// Main tab navigation for the Vantage companion app.
@available(iOS 16.0, *)
struct ContentView: View {

    @EnvironmentObject private var session: SessionManager
    @EnvironmentObject private var backend: BackendManager

    @State private var selectedTab = 0

    var body: some View {
        TabView(selection: $selectedTab) {
            HomeView()
                .tabItem {
                    Label("Home", systemImage: "house.fill")
                }
                .tag(0)

            CommandView()
                .tabItem {
                    Label("Command", systemImage: "terminal.fill")
                }
                .tag(1)

            ApprovalsView()
                .tabItem {
                    Label("Approvals", systemImage: "checkmark.shield.fill")
                }
                .tag(2)
                .badge(pendingCount)

            DevicesView()
                .tabItem {
                    Label("Devices", systemImage: "iphone.gen3")
                }
                .tag(3)

            SettingsView()
                .tabItem {
                    Label("Settings", systemImage: "gearshape.fill")
                }
                .tag(4)
        }
        .tint(.blue)
    }

    private var pendingCount: Int {
        backend.lastHealth?.pending_confirmations ?? 0
    }
}