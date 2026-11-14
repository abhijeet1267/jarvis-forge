import Foundation
import PDFKit
import UniformTypeIdentifiers

/// A document a person explicitly selected, and the text Vantage actually read
/// from it.
///
/// The pipeline is deliberately staged so every step can refuse on its own:
/// validate → read → extract → (later, if asked) send to a provider. The
/// `DocumentSource` carries no contents, so a failure at any stage is reported
/// against the file the person chose rather than against a blob.
struct DocumentSource: Identifiable, Equatable {
    let id = UUID()
    /// The URL the person selected. Shown as a name in the UI; the full path is
    /// kept for "Reveal in Finder" and never logged.
    let url: URL
    let displayName: String
    let byteSize: Int
    let kind: DocumentKind
    let modifiedAt: Date?

    var identity: String { url.path }
}

/// The formats this app can genuinely read.
///
/// Each case names the extractor that implements it. A format that is not here
/// is refused *by name* — "Vantage cannot read .docx in this build" — rather
/// than handed to a text decoder that would produce mojibake and call it text.
enum DocumentKind: String, CaseIterable {
    case plainText
    case markdown
    case pdf
    case rtf
    case html
    case sourceCode
    case csv

    var displayName: String {
        switch self {
        case .plainText: return "Plain text"
        case .markdown: return "Markdown"
        case .pdf: return "PDF"
        case .rtf: return "Rich text"
        case .html: return "Web page"
        case .sourceCode: return "Source code"
        case .csv: return "CSV"
        }
    }

    /// The extensions this kind accepts. Used both for the file picker and for
    /// validating a URL that arrived some other way.
    var extensions: [String] {
        switch self {
        case .plainText: return ["txt", "text", "log"]
        case .markdown: return ["md", "markdown"]
        case .pdf: return ["pdf"]
        case .rtf: return ["rtf", "rtfd"]
        case .html: return ["html", "htm", "xhtml"]
        case .sourceCode: return ["swift", "py", "js", "ts", "tsx", "jsx", "json", "yaml",
                                  "yml", "toml", "sh", "rb", "go", "rs", "java", "kt",
                                  "c", "h", "cpp", "css", "xml", "sql"]
        case .csv: return ["csv", "tsv"]
        }
    }

    static func kind(for url: URL) -> DocumentKind? {
        let ext = url.pathExtension.lowercased()
        guard !ext.isEmpty else { return nil }
        return allCases.first { $0.extensions.contains(ext) }
    }

    /// Everything the picker offers.
    static var allExtensions: [String] { allCases.flatMap(\.extensions).sorted() }
}

/// How a document's text was obtained, and what that means for its fidelity.
///
/// Shown to the person before any AI request, because "the PDF's text layer" and
/// "the PDF's rendered pages via OCR" are different levels of confidence and the
/// summary is only as good as the text behind it.
enum ExtractionMethod: Equatable {
    case directText
    case pdfTextLayer(pages: Int, totalPages: Int)
    case rtfDecoded
    case htmlStripped
    case ocr(language: String?)
    case empty

    var description: String {
        switch self {
        case .directText: return "Read as text."
        case .pdfTextLayer(let pages, let total):
            return pages == total
                ? "Read the PDF's text layer (\(total) page\(total == 1 ? "" : "s"))."
                : "Read the text layer from \(pages) of \(total) pages — the rest had no text."
        case .rtfDecoded: return "Decoded the rich text."
        case .htmlStripped: return "Read the visible text of the page."
        case .ocr(let language): return "No text layer; read the pages with OCR"
                + (language.map { " (\($0))" } ?? "") + "."
        case .empty: return "Read, and there was no text."
        }
    }

    /// Whether the extraction is a measurement worth trusting for a summary.
    var isReliable: Bool {
        switch self {
        case .directText, .pdfTextLayer, .rtfDecoded, .htmlStripped: return true
        case .ocr, .empty: return false
        }
    }
}

/// A document's extracted contents, bounded.
struct ExtractedDocument {
    let source: DocumentSource
    let text: String
    let method: ExtractionMethod
    /// True when the document was longer than the bound and `text` is the first
    /// part of it. The summary must say so rather than imply it saw everything.
    let truncated: Bool
    /// The number of characters actually kept.
    var characterCount: Int { text.count }

    var wordCount: Int { text.split(whereSeparator: \.isWhitespace).count }
}

/// Why a document could not be read. Every case names something a person can act
/// on; none of them is "failed".
enum DocumentError: LocalizedError, Equatable {
    case unsupported(String)
    case notAFile(String)
    case unreadable(String)
    case tooLarge(bytes: Int, limit: Int)
    case empty(String)
    case protected(String)
    case permissionDenied(String)

    var errorDescription: String? {
        switch self {
        case .unsupported(let ext):
            return "Vantage cannot read \(ext.isEmpty ? "that kind of file" : ".\(ext) files") "
                + "in this build. It reads PDF, plain text, Markdown, rich text, HTML, CSV "
                + "and source code. Convert it to PDF or text, or choose another file."
        case .notAFile(let name):
            return "\(name) is a folder, not a file. Choose a document."
        case .unreadable(let name):
            return "\(name) could not be read. It may have moved, or the app does not have "
                + "permission to open it."
        case .tooLarge(let bytes, let limit):
            let mb = Double(bytes) / 1_048_576
            let limitMB = Double(limit) / 1_048_576
            return String(format: "%@ is %.1f MB, over the %.0f MB limit for one document. ", "", mb, limitMB)
        case .empty(let name):
            return "\(name) was read and contains no text to work with."
        case .protected(let name):
            return "\(name) is password-protected, so its text cannot be read. Unlock it first."
        case .permissionDenied(let name):
            return "\(name) is in a location Vantage is not allowed to read. Choose it with "
                + "the file picker so macOS grants access for this file."
        }
    }
}