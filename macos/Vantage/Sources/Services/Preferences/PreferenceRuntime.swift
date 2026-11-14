import Foundation

/// Process-wide access to the one preference store.
@MainActor
final class PreferenceRuntime {

    static let shared = PreferenceRuntime()

    let store: PreferenceStore

    private init() {
        store = PreferenceStore()
    }
}

extension PreferenceStore {
    @MainActor
    static var shared: PreferenceStore { PreferenceRuntime.shared.store }
}