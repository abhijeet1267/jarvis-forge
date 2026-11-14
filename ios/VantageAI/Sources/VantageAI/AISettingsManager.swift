import Foundation
import Security

/// Persistent, secure configuration for the in-app AI client.
///
/// Split by sensitivity, on purpose:
///   * The **API key** is a secret and lives in the iOS Keychain.
///   * Everything else (`baseURL`, `selectedModel`, `systemPrompt`, `temperature`)
///     is ordinary preference data and lives in `UserDefaults`.
///
/// Nothing here writes the key to `UserDefaults`, a plist, or logs; the Keychain is
/// the only store that ever sees it. This mirrors the desktop app's rule (macOS
/// Keychain via the credential abstraction) so the two clients agree on where a
/// secret may live.
@MainActor
final class AISettingsManager: ObservableObject {

    /// Shared instance: the Siri intent and the settings view must read the *same*
    /// configuration, so there is exactly one source of truth in the process.
    static let shared = AISettingsManager()

    // MARK: - Keys

    private enum KeychainKey {
        static let service = "com.jarvisforge.vantage.ai"
        static let account = "api-key"
    }

    private enum DefaultsKey {
        static let baseURL = "vantage.ai.baseURL"
        static let selectedModel = "vantage.ai.selectedModel"
        static let systemPrompt = "vantage.ai.systemPrompt"
        static let temperature = "vantage.ai.temperature"
    }

    /// A safe default endpoint. The user can point this at any OpenAI-compatible
    /// service (OpenRouter, Groq, DeepSeek, Ollama, LM Studio, …).
    static let defaultBaseURL = "https://api.openai.com/v1"

    // MARK: - Published state (drives the SwiftUI form)

    @Published var baseURL: String {
        didSet { defaults.set(baseURL, forKey: DefaultsKey.baseURL) }
    }

    @Published var selectedModel: String {
        didSet { defaults.set(selectedModel, forKey: DefaultsKey.selectedModel) }
    }

    @Published var systemPrompt: String {
        didSet { defaults.set(systemPrompt, forKey: DefaultsKey.systemPrompt) }
    }

    @Published var temperature: Double {
        didSet { defaults.set(temperature, forKey: DefaultsKey.temperature) }
    }

    /// The in-memory copy of the key, kept only so the sheet can show whether a key
    /// is set. It is loaded from the Keychain on init and never written anywhere else.
    @Published private(set) var hasAPIKey: Bool

    private let defaults: UserDefaults

    private init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.baseURL = defaults.string(forKey: DefaultsKey.baseURL) ?? Self.defaultBaseURL
        self.selectedModel = defaults.string(forKey: DefaultsKey.selectedModel) ?? ""
        self.systemPrompt = defaults.string(forKey: DefaultsKey.systemPrompt)
            ?? "You are Vantage, a concise, helpful assistant."
        self.temperature = defaults.object(forKey: DefaultsKey.temperature) as? Double ?? 0.7
        self.hasAPIKey = Self.readKeyFromKeychain() != nil
    }

    // MARK: - API key (Keychain)

    /// Store or replace the API key. Returns true only if the Keychain accepted it;
    /// a failure never silently falls back to insecure storage.
    @discardableResult
    func setAPIKey(_ key: String) -> Bool {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: KeychainKey.service,
            kSecAttrAccount as String: KeychainKey.account,
        ]
        // Replace any existing item rather than accumulating duplicates.
        SecItemDelete(query as CFDictionary)

        var attributes = query
        attributes[kSecValueData as String] = Data(trimmed.utf8)
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        // Non-secret metadata so a future reader knows what this item is without
        // needing the value (and a config looks self-describing in Keychain Access).
        attributes[kSecAttrLabel as String] = "Vantage AI API key"

        let status = SecItemAdd(attributes as CFDictionary, nil)
        let ok = status == errSecSuccess
        if ok { hasAPIKey = true }
        return ok
    }

    /// The stored key, or nil. Reads the Keychain each time so a key changed in
    /// another process/extension is not masked by a stale in-memory copy.
    func apiKey() -> String? {
        Self.readKeyFromKeychain()
    }

    /// Remove the stored key. Returns true if the item existed and is now gone.
    @discardableResult
    func deleteAPIKey() -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: KeychainKey.service,
            kSecAttrAccount as String: KeychainKey.account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        let ok = status == errSecSuccess || status == errSecItemNotFound
        hasAPIKey = false
        return ok
    }

    private static func readKeyFromKeychain() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: KeychainKey.service,
            kSecAttrAccount as String: KeychainKey.account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8),
              !value.isEmpty
        else { return nil }
        return value
    }
}