import Foundation

// MARK: - Health

/// Backend health response from GET /health
public struct HealthResponse: Codable, Sendable {
    public let status: String
    public let tools: Int
    public let agents: [String]
    public let llm_enabled: Bool
    public let llm_source: String?
    public let ai_provider: String?
    public let provider_paths: [String: String]?
    public let pending_confirmations: Int?
    public let phone_urls: [String]?
    public let security: [String: String]?
    public let privacy: [String: String]?
    public let devices: DeviceHealthInfo?
    public let peripherals: String?
    public let integrations: [String: [String: String]]?
    public let memory: MemoryHealthInfo?
    public let system: SystemHealthInfo?
}

public struct DeviceHealthInfo: Codable, Sendable {
    public let pairing: [String: AnyCodable]?
    public let transport: String?
    public let paired_devices: Int?
}

public struct MemoryHealthInfo: Codable, Sendable {
    public let items: Int?
    public let projects: [String]?
}

public struct SystemHealthInfo: Codable, Sendable {
    public let hostname: String?
    public let platform: String?
}

// MARK: - Command

public struct CommandRequest: Codable, Sendable {
    public let text: String
    public let session_id: String?
    public let device_id: String
    public let source: String
    public let context: [String: String]

    public init(text: String, deviceId: String = "phone", sessionId: String? = nil) {
        self.text = text
        self.session_id = sessionId
        self.device_id = deviceId
        self.source = "mobile"
        self.context = [:]
    }
}

public struct CommandResponse: Codable, Sendable {
    public let result: String?
    public let status: String?
    public let error: String?
    public let task_id: String?
    public let tool_calls: [String]?
    public let verification: VerificationResult?
    public let confirmation_required: Bool?
    public let confirmation_id: String?
}

public struct VerificationResult: Codable, Sendable {
    public let status: String?
    public let verifications_passed: Int?
    public let verifications_failed: Int?
}

// MARK: - Confirmation

public struct Confirmation: Codable, Sendable, Identifiable {
    public let id: String
    public let action: String?
    public let target: String?
    public let parameters: [String: AnyCodable]?
    public let risk: String?
    public let status: String?
    public let created_at: String?
    public let expires_at: String?
    public let source: String?
    public let detail: [String: AnyCodable]?

    /// Display-friendly action description
    public var actionDescription: String {
        action ?? "unknown"
    }

    /// Display-friendly target
    public var targetDescription: String {
        target ?? "unspecified"
    }

    /// Risk level color hint: "low" | "medium" | "high"
    public var riskLevel: String {
        risk?.lowercased() ?? "medium"
    }
}

// MARK: - Device

public struct DeviceInfo: Codable, Sendable, Identifiable {
    public let id: String
    public let device_id: String?
    public let device_name: String?
    public let state: String?
    public let paired_at: String?
    public let last_seen: String?
    public let surface: String?
    public let fingerprint: String?

    public var displayName: String {
        device_name ?? device_id ?? id
    }

    public var isPaired: Bool {
        state == "paired"
    }

    public var isPhone: Bool {
        surface == "phone"
    }
}

// MARK: - Pairing

public struct PairingRequest: Codable, Sendable {
    public let device_id: String
    public let device_name: String
    public let public_key: String

    public init(deviceId: String, deviceName: String, publicKey: String = "") {
        self.device_id = deviceId
        self.device_name = deviceName
        self.public_key = publicKey
    }
}

public struct PairingResponse: Codable, Sendable {
    public let pairing_id: String?
    public let device_id: String?
    public let state: String?
    public let message: String?
}

public struct PendingPairing: Codable, Sendable, Identifiable {
    public let pairing_id: String?
    public let device_id: String?
    public let device_name: String?
    public let fingerprint: String?
    public let state: String?
    public let requested_at: String?

    public var id: String { pairing_id ?? device_id ?? UUID().uuidString }
}

// MARK: - Session

public struct SessionResponse: Codable, Sendable {
    public let auth_enabled: Bool
    public let session_token: String?
    public let expires_in: Double?
    public let surface: String?
}

public struct TokenResponse: Codable, Sendable {
    public let auth_enabled: Bool?
    public let token: String?
    public let expires_in: Double?
}

// MARK: - Task

public struct TaskInfo: Codable, Sendable, Identifiable {
    public let id: String?
    public let task_id: String?
    public let intent: String?
    public let status: String?
    public let created_at: String?
    public let completed_at: String?
    public let result: String?
    public let error: String?

    public var effectiveId: String { task_id ?? id ?? UUID().uuidString }
    public var isRunning: Bool { status == "running" || status == "pending" }
}

public struct TaskListResponse: Codable, Sendable {
    public let tasks: [TaskInfo]?
    public let count: Int?
}

// MARK: - Audit

public struct AuditEntry: Codable, Sendable, Identifiable {
    public let id: String?
    public let intent: String?
    public let tool: String?
    public let result: String?
    public let risk: String?
    public let timestamp: String?

    public var effectiveId: String { id ?? UUID().uuidString }
}

// MARK: - Project

public struct ProjectInfo: Codable, Sendable, Identifiable {
    public let id: String?
    public let name: String?
    public let created_at: String?
    public let updated_at: String?
}

// MARK: - Memory

public struct MemoryEntry: Codable, Sendable, Identifiable {
    public let id: String?
    public let content: String?
    public let scope: String?
    public let project: String?
    public let created_at: String?

    public var effectiveId: String { id ?? UUID().uuidString }
}

// MARK: - Voice

public struct VoiceStatus: Codable, Sendable {
    public let active: Bool?
    public let listening: Bool?
    public let mode: String?
}

public struct VoiceTranscription: Codable, Sendable {
    public let text: String?
    public let confidence: Double?
}

// MARK: - Tools

public struct ToolInfo: Codable, Sendable, Identifiable {
    public let name: String?
    public let description: String?
    public let category: String?

    public var id: String { name ?? UUID().uuidString }
}

// MARK: - API Error

public struct APIErrorResponse: Codable, Sendable {
    public let detail: String?
    public let error: String?

    /// Best available error message
    public var message: String {
        detail ?? error ?? "Unknown error"
    }
}

// MARK: - AnyCodable

/// Flexible JSON value holder for dynamic fields
public enum AnyCodable: Codable, Sendable, Equatable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case dictionary([String: AnyCodable])
    case array([AnyCodable])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let v = try? container.decode(String.self) { self = .string(v) }
        else if let v = try? container.decode(Int.self) { self = .int(v) }
        else if let v = try? container.decode(Double.self) { self = .double(v) }
        else if let v = try? container.decode(Bool.self) { self = .bool(v) }
        else if let v = try? container.decode([String: AnyCodable].self) { self = .dictionary(v) }
        else if let v = try? container.decode([AnyCodable].self) { self = .array(v) }
        else { self = .null }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let v): try container.encode(v)
        case .int(let v): try container.encode(v)
        case .double(let v): try container.encode(v)
        case .bool(let v): try container.encode(v)
        case .dictionary(let v): try container.encode(v)
        case .array(let v): try container.encode(v)
        case .null: try container.encodeNil()
        }
    }
}