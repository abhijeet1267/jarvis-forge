import Foundation

/// A document the app has read and keeps available to several screens.
///
/// Deliberately a small value plus the extracted text, not a cache of every file
/// ever touched: it lives only as long as the Documents screen's own state, so
/// closing the screen releases the text. The brief forbids silent whole-disk
/// indexing, and this type is the concrete reason nothing like it exists here.
struct DocumentSession: Identifiable {
    let id = UUID()
    let document: ExtractedDocument
    var results: [DocumentResult] = []
    var proposals: [ProposedAction] = []
    var proposalProblem: String?
    var isWorking = false
    var lastError: String?

    var sourceName: String { document.source.displayName }
}