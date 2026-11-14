import Combine
import Foundation

// MARK: - Client Error

public enum CompanionClientError: LocalizedError, Equatable {
    case notConfigured
    case invalidURL(String)
    case badStatus(Int, String)
    case emptyResponse
    case transport(String)
    case decoding(String)
    case unauthorized
    case sessionExpired
    case tokenRevoked

    public var errorDescription: String? {
        switch self {
        case .notConfigured: return "Backend URL not configured."
        case .invalidURL(let v): return "Invalid URL: \(v)"
        case .badStatus(let code, let body):
            let p = String(body.trimmingCharacters(in: .whitespacesAndNewlines).prefix(200))
            return "Server returned \(code). \(p)"
        case .emptyResponse: return "The server returned an empty response."
        case .transport(let m): return "Could not reach the server: \(m)"
        case .decoding(let m): return "Failed to parse response: \(m)"
        case .unauthorized: return "Not authorized. Re-pair from Settings."
        case .sessionExpired: return "Session expired. Re-authenticate."
        case .tokenRevoked: return "Token revoked."
        }
    }
}

// MARK: - Client

@MainActor
public final class CompanionAPIClient: ObservableObject {

    public static let shared = CompanionAPIClient()

    private let urlSession: URLSession
    private let sessionManager: SessionManager
    private let timeout: TimeInterval = 30
    private let decoder = JSONDecoder()

    public init(sessionManager: SessionManager = .shared) {
        self.sessionManager = sessionManager
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 60
        config.httpAdditionalHeaders = [
            "User-Agent": "VantageCompanion/1.0 iOS",
            "Accept": "application/json",
        ]
        self.urlSession = URLSession(configuration: config)
    }

    // MARK: - Backend URL

    var backendHost: String {
        let raw = sessionManager.backendURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "http://\(raw)"), let host = url.host else { return raw }
        if let port = url.port, port != 80, port != 443 { return "\(host):\(port)" }
        return host
    }

    private func buildURL(_ path: String) throws -> URL {
        let base = sessionManager.backendURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !base.isEmpty else { throw CompanionClientError.notConfigured }
        let full = "http://\(base)\(path)"
        guard let url = URL(string: full) else { throw CompanionClientError.invalidURL(full) }
        return url
    }

    // MARK: - Auth header

    private func applyAuth(_ req: inout URLRequest) {
        if let token = sessionManager.sessionToken(), !token.isEmpty {
            req.setValue(token, forHTTPHeaderField: "X-JARVIS-Token")
        }
    }

    // MARK: - Request executor

    private func request<T: Decodable>(_ method: String, _ path: String, body: Data? = nil, extraHeaders: [String: String] = [:]) async throws -> T {
        let url = try buildURL(path)
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = method
        req.httpBody = body
        if body != nil { req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        applyAuth(&req)
        for (k, v) in extraHeaders { req.setValue(v, forHTTPHeaderField: k) }

        let (data, resp) = try await urlSession.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw CompanionClientError.transport("Invalid response")
        }
        switch http.statusCode {
        case 200...299: break
        case 401: throw CompanionClientError.unauthorized
        case 403: throw CompanionClientError.tokenRevoked
        default:
            let body = String(data: data, encoding: .utf8) ?? ""
            throw CompanionClientError.badStatus(http.statusCode, body)
        }
        guard !data.isEmpty else { throw CompanionClientError.emptyResponse }
        do { return try decoder.decode(T.self, from: data) }
        catch {
            let s = String(data: data, encoding: .utf8)?.prefix(300) ?? ""
            throw CompanionClientError.decoding("\(error.localizedDescription) — \(s)")
        }
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try await request("GET", path)
    }

    private func post<T: Decodable>(_ path: String, body: (some Encodable)? = nil, extraHeaders: [String: String] = [:]) async throws -> T {
        let data = try body.map { try JSONEncoder().encode($0) }
        return try await request("POST", path, body: data, extraHeaders: extraHeaders)
    }

    // MARK: - Public API

    public func fetchHealth() async throws -> HealthResponse {
        try await get("/health")
    }

    public func exchangeBootstrap(code: String) async throws -> SessionResponse {
        let resp: SessionResponse = try await post("/auth/session",
            body: ["bootstrap": code] as [String: String],
            extraHeaders: ["Origin": "http://\(backendHost)"])
        if let token = resp.session_token {
            sessionManager.saveSession(token: token, surface: resp.surface ?? "phone")
        }
        return resp
    }

    public func sendCommand(_ cmd: CommandRequest) async throws -> CommandResponse {
        try await post("/command", body: cmd)
    }

    public func fetchConfirmations() async throws -> [Confirmation] {
        try await get("/confirmations")
    }

    public func approveConfirmation(_ id: String) async throws -> [String: AnyCodable] {
        try await post("/confirm/\(id)/approve" as String)
    }

    public func denyConfirmation(_ id: String) async throws -> [String: AnyCodable] {
        try await post("/confirm/\(id)/deny" as String)
    }

    public func fetchDevices() async throws -> [DeviceInfo] {
        try await get("/devices")
    }

    public func requestPairing(deviceId: String, deviceName: String) async throws -> PairingResponse {
        try await post("/devices/pair/request", body: PairingRequest(deviceId: deviceId, deviceName: deviceName, publicKey: ""))
    }

    public func emergencyStop() async throws -> [String: AnyCodable] {
        try await post("/emergency_stop" as String)
    }

    public func signOut() async throws -> [String: String] {
        let resp: [String: String] = try await post("/auth/token")
        return resp
    }

    public func fetchTasks() async throws -> TaskListResponse {
        try await get("/tasks")
    }

    public func fetchMemory() async throws -> [MemoryEntry] {
        try await get("/memory")
    }

    public func fetchTools() async throws -> [ToolInfo] {
        try await get("/tools")
    }

    public func fetchVoiceStatus() async throws -> VoiceStatus {
        try await get("/voice/status")
    }

    public func fetchBootstrapFromPhonePage() async throws -> String {
        let url = try buildURL("/phone")
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        req.httpMethod = "GET"
        let (data, resp) = try await urlSession.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200...299).contains(http.statusCode),
              let html = String(data: data, encoding: .utf8) else {
            throw CompanionClientError.transport("Could not fetch phone page")
        }
        if let match = try? NSRegularExpression(pattern: #"name="jarvis-bootstrap"\s+content="([^"]*)""#)
            .firstMatch(in: html, range: NSRange(html.startIndex..., in: html)),
           let r = Range(match.range(at: 1), in: html) {
            return String(html[r])
        }
        return ""
    }
}