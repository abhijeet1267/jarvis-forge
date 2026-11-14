import SwiftUI

/// One place that decides which screen is showing, so a feature can hand off to
/// another without reaching into the view that owns the sidebar.
///
/// Before this, `ContentView` held the selection in a private `@State` and the
/// only way across was a Notification whose payload was a string. A capture item
/// that converts into a flow draft needs to hand over the *draft itself* (not a
/// name to re-parse), and a briefing card needs to open a specific run — typed
/// payloads, not prose, is what makes those handoffs honest.
@MainActor
final class AppNavigation: ObservableObject {

    static let shared = AppNavigation()

    /// Set to switch screens. `ContentView` consumes it and clears it, so a
    /// re-render cannot silently re-navigate to a screen the person left.
    @Published private(set) var request: Request?

    struct Request: Identifiable, Equatable {
        let id = UUID()
        let destination: NavItem
        /// A flow drafted by another feature (capture, a document's action list,
        /// a saved sequence). The Flows screen shows it as an *unsaved* draft —
        /// nothing here saves or runs it.
        var flowDraft: Flow?
        /// A capture item to select when the inbox opens.
        var captureID: UUID?
        /// A flow whose history row should be expanded, for "review the failed
        /// flow" from the briefing.
        var flowID: UUID?
        /// Text to prefill a composer with. Never submitted automatically.
        var prefill: String?
    }

    private init() {}

    func go(_ destination: NavItem, flowDraft: Flow? = nil, captureID: UUID? = nil,
            flowID: UUID? = nil, prefill: String? = nil) {
        request = Request(destination: destination, flowDraft: flowDraft,
                          captureID: captureID, flowID: flowID, prefill: prefill)
    }

    func consume() { request = nil }
}