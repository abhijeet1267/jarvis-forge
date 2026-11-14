import Foundation
import Network
import Combine

// MARK: - Connection State

public enum ConnectionState: Equatable, Sendable {
    case disconnected
    case discovering
    case connecting(String)  // host:port
    case connected(HealthResponse)
    case error(String)

    public var label: String {
        switch self {
        case .disconnected: return "Disconnected"
        case .discovering: return "Searching…"
        case .connecting(let host): return "Connecting to \(host)…"
        case .connected: return "Connected"
        case .error: return "Error"
        }
    }

    public var isConnected: Bool {
        if case .connected = self { return true }
        return false
    }
}

// MARK: - Backend Manager

/// Manages connection to the Vantage Mac backend.
///
/// Supports:
/// - Bonjour/mDNS discovery on local network
/// - Manual IP/hostname entry
/// - Health check polling
/// - Auto-reconnect after disconnection
@MainActor
public final class BackendManager: ObservableObject {

    public static let shared = BackendManager()

    @Published public var state: ConnectionState = .disconnected
    @Published public var lastHealth: HealthResponse?
    @Published public var discoveredHosts: [String] = []

    private let client: CompanionAPIClient
    private let sessionManager: SessionManager
    private var healthTimer: Timer?
    private var browser: NWBrowser?
    private let healthInterval: TimeInterval = 10

    public init(client: CompanionAPIClient = .shared, sessionManager: SessionManager = .shared) {
        self.client = client
        self.sessionManager = sessionManager
    }

    // MARK: - Manual Connect

    /// Connect to a backend at the given host (e.g., "192.168.1.100:8765")
    public func connect(to host: String) async {
        state = .connecting(host)
        sessionManager.saveBackendURL(host)

        do {
            let health = try await client.fetchHealth()
            state = .connected(health)
            lastHealth = health
            startHealthPolling()
        } catch {
            let msg = (error as? CompanionClientError)?.errorDescription ?? error.localizedDescription
            state = .error(msg)
        }
    }

    /// Disconnect from the backend
    public func disconnect() {
        stopHealthPolling()
        stopDiscovery()
        state = .disconnected
        lastHealth = nil
    }

    // MARK: - mDNS Discovery

    /// Start browsing for Vantage on the local network via Bonjour
    public func startDiscovery() {
        stopDiscovery()

        state = .discovering

        let parameters = NWParameters()
        parameters.includePeerToPeer = true

        browser = NWBrowser(for: .bonjour(type: "_http._tcp", domain: "local."), using: parameters)

        browser?.browseResultsChanged = { [weak self] results, _ in
            Task { @MainActor in
                self?.handleDiscoveryResults(results)
            }
        }

        browser?.stateUpdateHandler = { [weak self] state in
            if case .failed = state {
                Task { @MainActor in
                    self?.stopDiscovery()
                }
            }
        }

        browser?.start(queue: .main)
    }

    public func stopDiscovery() {
        browser?.cancel()
        browser = nil
    }

    /// Probe discovered host to verify it serves the Vantage health endpoint
    public func probeHost(_ host: String) async -> Bool {
        let origURL = sessionManager.backendURL
        sessionManager.saveBackendURL(host)
        do {
            let health = try await client.fetchHealth()
            if health.status == "ok" {
                sessionManager.saveBackendURL(origURL)
                return true
            }
        } catch { }
        sessionManager.saveBackendURL(origURL)
        return false
    }

    // MARK: - Health Polling

    private func startHealthPolling() {
        stopHealthPolling()
        healthTimer = Timer.scheduledTimer(withTimeInterval: healthInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                await self?.checkHealth()
            }
        }
    }

    private func stopHealthPolling() {
        healthTimer?.invalidate()
        healthTimer = nil
    }

    private func checkHealth() async {
        guard sessionManager.hasSession else { return }
        do {
            let health = try await client.fetchHealth()
            if case .connected = state {
                state = .connected(health)
            }
            lastHealth = health
        } catch {
            // Don't change state on transient failures — just record
            lastHealth = nil
        }
    }

    // MARK: - Discovery Helpers

    private func handleDiscoveryResults(_ results: Set<NWBrowser.Result>) {
        var hosts: Set<String> = []
        for result in results {
            switch result.endpoint {
            case .service(let name, _, _, _):
                // mDNS service names include the host; extract the hostname
                // Typical format: "Vantage._http._tcp.local."
                if name.lowercased().contains("vantage") || name.lowercased().contains("jarvis") {
                    if let host = extractHost(from: result) {
                        hosts.insert(host)
                    }
                }
            default:
                break
            }
        }
        discoveredHosts = Array(hosts).sorted()
    }

    private func extractHost(from result: NWBrowser.Result) -> String? {
        switch result.endpoint {
        case .service(let name, _, _, _):
            // The endpoint string often looks like "Hostname.local:8765"
            let epString = result.endpoint.debugDescription
            // Try to extract host:port
            if let range = epString.range(of: "\\d+\\.\\d+\\.\\d+\\.\\d+:\\d+", options: .regularExpression) {
                return String(epString[range])
            }
            // Fallback: service name
            return "\(name).local:8765"
        default:
            return nil
        }
    }
}