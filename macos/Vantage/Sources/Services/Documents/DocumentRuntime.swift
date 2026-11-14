import Foundation

/// Process-wide access to the documents the app currently has open.
///
/// Only two things live here: the open sessions and their extracted text, and
/// the names the palette is allowed to know. Nothing scans a folder, nothing
/// watches a file, and closing the app discards the text — the brief's rule that
/// there is no invisible background indexing is enforced by there being no
/// place to put one.
@MainActor
final class DocumentRuntime: ObservableObject {

    static let shared = DocumentRuntime()

    /// The documents open in the Documents screen. Defaults to just one; the
    /// comparison view adds a second.
    @Published private(set) var sessions: [DocumentSession] = []
    /// The most recent result, so the palette's "ask about <doc>" can be routed
    /// to the screen that holds the text rather than answered from a name.
    @Published private(set) var lastQuestion: String = ""

    private init() {}

    /// Names the palette may match against a sentence. Deliberately just names.
    var openDocumentNames: [String] {
        sessions.map(\.sourceName)
    }

    var primary: DocumentSession? { sessions.first }

    func setPrimary(_ document: ExtractedDocument) {
        if let index = sessions.firstIndex(where: {
            $0.document.source.identity == document.source.identity
        }) {
            sessions[index] = DocumentSession(document: document)
        } else if sessions.isEmpty {
            sessions = [DocumentSession(document: document)]
        } else {
            sessions[0] = DocumentSession(document: document)
        }
    }

    func addComparison(_ document: ExtractedDocument) {
        guard !sessions.contains(where: {
            $0.document.source.identity == document.source.identity
        }) else { return }
        sessions.append(DocumentSession(document: document))
    }

    func remove(_ id: UUID) {
        sessions.removeAll { $0.id == id }
    }

    func session(_ id: UUID) -> DocumentSession? {
        sessions.first { $0.id == id }
    }

    func update(_ session: DocumentSession) {
        guard let index = sessions.firstIndex(where: { $0.id == session.id }) else { return }
        sessions[index] = session
    }

    func clear() { sessions = [] }

    /// Remember a question the palette handed over, so the screen can prefill
    /// its field with it (never submit it).
    func handOffQuestion(_ text: String) {
        lastQuestion = text
    }

    func consumeQuestion() -> String {
        let text = lastQuestion
        lastQuestion = ""
        return text
    }
}