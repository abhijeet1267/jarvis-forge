import Foundation

/// Process-wide access to the one capture inbox.
///
/// Same shape as `FlowRuntime`: the palette, the Inbox screen, the briefing and
/// the Command Center all need the *same* items, and a second store would mean
/// two answers to "what is in my inbox".
@MainActor
final class CaptureRuntime {

    static let shared = CaptureRuntime()

    let store: CaptureStore
    let classifier = CaptureClassifier()

    private init() {
        store = CaptureStore()
    }
}

extension CaptureStore {
    @MainActor
    static var shared: CaptureStore { CaptureRuntime.shared.store }
}