import Combine
import Foundation
import Security

/// Secure credential storage using the iOS Keychain.
///
/// All secrets (session tokens) are stored in the Keychain and never written to
/// UserDefaults, plists, or logs. This mirrors the desktop app's rule that a
/// secret may only live in platform-secure storage.
///
/// The existing `AISettingsManager` Keychain usage in this project provides the
/// pattern: `kSecClassGenericPassword`, service + account pair, query-read-write.
@MainActor
public final class SessionManager: ObservableObject {

    public static let shared = SessionManager()

    // MARK: - Keys

    private enum Key {
        static let service = "com.jarvisforge.vantage.session"
    }

    private enum Account {
        static let sessionToken = "session-token"
        static let surface = "surface"
        static let backendURL = "backend-url"
        static let deviceId = "device-id"
        static let deviceName = "device-name"
        static let pairingId = "pairing-id"
    }

    // MARK: - Published state

    @Published public private(set) var surface: String = ""
    @Published public private(set) var backendURL: String = ""
    @Published public private(set) var deviceId: String = ""
    @Published public private(set) var deviceName: String = ""
    @Published public private(set) var pairingId: String = ""
    @Published public private(set) var hasSession: Bool = false

    // MARK: - Init

    public init() {
        reload()
    }

    // MARK: - Public API

    /// Store a session token obtained from POST /auth/session
    public func saveSession(token: String, surface: String) {
        saveKeychain(Account.sessionToken, token)
        saveDefaults(Account.surface, surface)

        self.surface = surface
        self.hasSession = true
    }

    /// Store backend connection URL
    public func saveBackendURL(_ url: String) {
        saveDefaults(Account.backendURL, url)
        self.backendURL = url
    }

    /// Store device identity
    public func saveDeviceIdentity(id: String, name: String) {
        saveDefaults(Account.deviceId, id)
        saveDefaults(Account.deviceName, name)
        self.deviceId = id
        self.deviceName = name
    }

    /// Store pairing ID
    public func savePairingId(_ id: String) {
        saveDefaults(Account.pairingId, id)
        self.pairingId = id
    }

    /// Retrieve the stored session token, or nil
    public func sessionToken() -> String? {
        readKeychain(Account.sessionToken)
    }

    /// Clear all session state (sign out)
    public func signOut() {
        deleteKeychain(Account.sessionToken)

        let defaults = UserDefaults.standard
        [Account.surface, Account.backendURL, Account.pairingId].forEach {
            defaults.removeObject(forKey: vaultedKey($0))
        }

        self.surface = ""
        self.backendURL = ""
        self.pairingId = ""
        self.hasSession = false
    }

    /// Reload published state from storage
    public func reload() {
        surface = readDefaults(Account.surface)
        backendURL = readDefaults(Account.backendURL)
        deviceId = readDefaults(Account.deviceId)
        deviceName = readDefaults(Account.deviceName)
        pairingId = readDefaults(Account.pairingId)
        hasSession = readKeychain(Account.sessionToken) != nil
    }

    // MARK: - Keychain helpers

    private func vaultedKey(_ key: String) -> String {
        "com.jarvisforge.vantage.\(key)"
    }

    private func saveKeychain(_ key: String, _ value: String) {
        deleteKeychain(key)

        guard let data = value.data(using: .utf8) else { return }

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Key.service,
            kSecAttrAccount as String: key,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        ]

        SecItemAdd(query as CFDictionary, nil)
    }

    private func readKeychain(_ key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Key.service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)

        guard status == errSecSuccess,
              let data = result as? Data,
              let string = String(data: data, encoding: .utf8) else {
            return nil
        }
        return string
    }

    private func deleteKeychain(_ key: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Key.service,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
    }

    // MARK: - UserDefaults helpers

    private func saveDefaults(_ key: String, _ value: String) {
        UserDefaults.standard.set(value, forKey: vaultedKey(key))
    }

    private func readDefaults(_ key: String) -> String {
        UserDefaults.standard.string(forKey: vaultedKey(key)) ?? ""
    }
}