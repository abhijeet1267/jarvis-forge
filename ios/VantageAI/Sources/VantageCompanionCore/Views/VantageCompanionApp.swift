import SwiftUI

/// Native iOS companion app for Vantage / Jarvis Forge.
///
/// Entry point: navigates to Setup for first-launch pairing, or the main
/// tab interface once a session has been established.
@available(iOS 16.0, *)
public struct VantageCompanionApp: View {

    @StateObject private var session = SessionManager.shared
    @StateObject private var backend = BackendManager.shared

    public init() {}

    public var body: some View {
        Group {
            if session.hasSession, case .connected = backend.state {
                ContentView()
            } else {
                SetupView()
            }
        }
        .environmentObject(session)
        .environmentObject(backend)
        .preferredColorScheme(.dark)
    }
}