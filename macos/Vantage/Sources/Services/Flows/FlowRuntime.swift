import SwiftUI
import AppKit

/// Process-wide singletons for the flow system.
///
/// One store and one engine for the whole app. The brief's core requirement is
/// that the palette, the Flows screen, Command Center and the runner share one
/// execution path; a second `FlowEngine` would be a second scheduler for the same
/// flows, so everything reaches the same instance through here.
///
/// `ObservableObject` so a view can observe the shared store without rebuilding
/// it: `FlowStore.shared` is stable for the process lifetime.
@MainActor
final class FlowRuntime {

    static let shared = FlowRuntime()

    let store: FlowStore
    let engine: FlowEngine

    /// Set by the Flows screen so a palette/Command-Center run that needs
    /// approval can bring that screen forward. Deliberately a closure rather than
    /// the view poking at the engine: the engine stays unaware of UI.
    var focusApproval: () -> Void = {}

    private init() {
        let store = FlowStore()
        self.store = store
        self.engine = FlowEngine(store: store)
    }

    /// Installed once by the root view, which owns navigation.
    func attach(focusApproval: @escaping () -> Void) {
        self.focusApproval = focusApproval
    }
}

extension FlowStore {
    /// The one store the app uses. Main-actor because the store is created and
    /// mutated by UI-driven work; every reader is on the main actor.
    @MainActor
    static var shared: FlowStore { FlowRuntime.shared.store }
}