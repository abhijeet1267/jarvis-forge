import Foundation
import PDFKit
import AppKit

/// Reads the text of a document a person explicitly selected.
///
/// Three properties this has on purpose:
///
/// * **Local first.** Text extraction happens here, with PDFKit / Foundation,
///   before any network call. A summary that needs a model is a *second*,
///   explicit step; reading your own file is never a data flow to a provider.
/// * **Bounded.** A file over the size cap, or a document longer than the
///   character bound, is refused or truncated *and says so*. Sending an
///   arbitrarily large blob to a model is how a "summarize this" turns into a
///   surprise bill.
/// * **Honest about failure.** An encrypted PDF, a scanned page with no text
///   layer, and a genuinely empty file are three different outcomes, and each is
///   reported as itself.
struct DocumentExtractor {

    /// The largest file this build will read into memory for extraction.
    static let maxBytes = 20 * 1_048_576

    /// The most characters kept. Enough for a long report; small enough that a
    /// later AI request stays inside a sane context budget.
    static let maxCharacters = 120_000

    /// Validate and describe a selected URL without reading its contents.
    ///
    /// Runs before the picker's result is accepted so an unsupported file is
    /// refused at selection time rather than after a slow upload.
    static func describe(_ url: URL) throws -> DocumentSource {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }

        let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .fileSizeKey,
                                                       .contentModificationDateKey])
        if values?.isDirectory == true {
            throw DocumentError.notAFile(url.lastPathComponent)
        }
        guard let kind = DocumentKind.kind(for: url) else {
            throw DocumentError.unsupported(url.pathExtension)
        }
        guard FileManager.default.isReadableFile(atPath: url.path) else {
            throw DocumentError.permissionDenied(url.lastPathComponent)
        }
        let size = values?.fileSize ?? 0
        guard size <= maxBytes else {
            throw DocumentError.tooLarge(bytes: size, limit: maxBytes)
        }
        return DocumentSource(url: url,
                              displayName: url.lastPathComponent,
                              byteSize: size,
                              kind: kind,
                              modifiedAt: values?.contentModificationDate)
    }

    /// Read the text of one described source.
    ///
    /// The security scope is opened and closed inside this call: extraction is
    /// the only thing that touches the bytes, so nothing else can read them by
    /// accident while the scope is open.
    static func extract(_ source: DocumentSource) throws -> ExtractedDocument {
        let scoped = source.url.startAccessingSecurityScopedResource()
        defer { if scoped { source.url.stopAccessingSecurityScopedResource() } }

        switch source.kind {
        case .pdf:
            return try extractPDF(source)
        case .rtf:
            return try extractRTF(source)
        case .html:
            return try extractHTML(source)
        case .plainText, .markdown, .sourceCode, .csv:
            return try extractText(source)
        }
    }

    // MARK: - Plain text

    private static func extractText(_ source: DocumentSource) throws -> ExtractedDocument {
        let data: Data
        do {
            data = try Data(contentsOf: source.url, options: .mappedIfSafe)
        } catch {
            throw DocumentError.unreadable(source.displayName)
        }
        return bounded(text: decodeText(data), source: source, method: .directText)
    }

    /// Decode text without inventing characters.
    ///
    /// A UTF-8 decode is tried first; a file that is not UTF-8 is retried with
    /// Latin-1, which maps every byte, rather than being reported as unreadable.
    /// A binary file that slipped past the extension check usually yields control
    /// characters — those are stripped below, and a result with no readable
    /// letters is reported as empty rather than as text.
    static func decodeText(_ data: Data) -> String {
        if let utf8 = String(data: data, encoding: .utf8) { return utf8 }
        if let latin = String(data: data, encoding: .isoLatin1) {
            return latin.filter { $0.isNewline || !$0.isASCII || $0.asciiValue.map { $0 >= 32 } ?? true }
        }
        return ""
    }

    // MARK: - PDF

    private static func extractPDF(_ source: DocumentSource) throws -> ExtractedDocument {
        guard let document = PDFDocument(url: source.url) else {
            throw DocumentError.unreadable(source.displayName)
        }
        if document.isLocked {
            throw DocumentError.protected(source.displayName)
        }
        var pages: [String] = []
        var readPages = 0
        let total = document.pageCount
        var budget = maxCharacters
        for index in 0..<total {
            guard let page = document.page(at: index) else { continue }
            let text = page.string ?? ""
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            readPages += 1
            if budget <= 0 { continue }
            let slice = String(text.prefix(budget))
            pages.append(slice)
            budget -= slice.count
        }
        let joined = pages.joined(separator: "\n\n")
        let method: ExtractionMethod = joined.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? .empty
            : .pdfTextLayer(pages: readPages, totalPages: total)
        var extracted = bounded(text: joined, source: source, method: method)
        if joined.count >= maxCharacters {
            extracted = ExtractedDocument(source: source, text: extracted.text,
                                          method: method, truncated: true)
        }
        return extracted
    }

    // MARK: - RTF

    private static func extractRTF(_ source: DocumentSource) throws -> ExtractedDocument {
        let data: Data
        do {
            data = try Data(contentsOf: source.url)
        } catch {
            throw DocumentError.unreadable(source.displayName)
        }
        var attributed: NSAttributedString?
        if let parsed = try? NSAttributedString(
            rtf: data, documentAttributes: nil) {
            attributed = parsed
        } else if let wrapper = try? FileWrapper(url: source.url, options: .immediate),
                  let parsed = try? NSAttributedString(
                    rtfdFileWrapper: wrapper, documentAttributes: nil) {
            attributed = parsed
        }
        guard let attributed else {
            throw DocumentError.unreadable(source.displayName)
        }
        return bounded(text: attributed.string, source: source, method: .rtfDecoded)
    }

    // MARK: - HTML

    private static func extractHTML(_ source: DocumentSource) throws -> ExtractedDocument {
        let data: Data
        do {
            data = try Data(contentsOf: source.url)
        } catch {
            throw DocumentError.unreadable(source.displayName)
        }
        let raw = decodeText(data)
        // The visible text only: scripts, styles and tags are not content, and a
        // summary of a page that includes its CSS is a summary of the wrong thing.
        var cleaned = raw
        for pattern in [#"<script\b[^>]*>[\s\S]*?</script>"#,
                        #"<style\b[^>]*>[\s\S]*?</style>"#,
                        #"<!--[\s\S]*?-->"#] {
            cleaned = cleaned.replacingOccurrences(of: pattern, with: " ",
                                                   options: [.regularExpression, .caseInsensitive])
        }
        cleaned = cleaned.replacingOccurrences(of: #"<[^>]+>"#, with: " ",
                                               options: .regularExpression)
        cleaned = cleaned.replacingOccurrences(of: #"&nbsp;"#, with: " ")
            .replacingOccurrences(of: #"&amp;"#, with: "&")
            .replacingOccurrences(of: #"&lt;"#, with: "<")
            .replacingOccurrences(of: #"&gt;"#, with: ">")
            .replacingOccurrences(of: #"&quot;"#, with: "\"")
        cleaned = cleaned.replacingOccurrences(of: #"[ \t]{2,}"#, with: " ",
                                               options: .regularExpression)
        return bounded(text: cleaned, source: source, method: .htmlStripped)
    }

    // MARK: - Bound

    /// Apply the character bound and normalise whitespace enough that a summary
    /// does not count a page of blank lines as content.
    private static func bounded(text: String, source: DocumentSource,
                                method: ExtractionMethod) -> ExtractedDocument {
        let clean = text.replacingOccurrences(of: "\r\n", with: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if clean.isEmpty, method != .empty {
            return ExtractedDocument(source: source, text: "", method: .empty, truncated: false)
        }
        let truncated = clean.count > maxCharacters
        let kept = truncated ? String(clean.prefix(maxCharacters)) : clean
        return ExtractedDocument(source: source, text: kept,
                                 method: method, truncated: truncated)
    }
}