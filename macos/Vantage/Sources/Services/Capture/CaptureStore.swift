import Foundation

/// The Quick Capture inbox, on disk.
///
/// Same conventions as `FlowStore` — one JSON file under
/// `~/Library/Application Support/Vantage/`, written atomically, schema-checked
/// on read — because a second persistence mechanism for a second small store
/// would be a second set of failure modes to get wrong.
///
/// The read rule is the important one: **a file that cannot be read is never
/// overwritten.** A decode failure moves the file aside with a timestamp,
/// starts the in-memory inbox empty, and publishes the fact. `save` refuses to
/// write while that condition holds, so a person whose file was momentarily
/// unreadable (a permissions glitch, a half-restored backup) does not lose the
/// inbox the first time they add one item.
final class CaptureStore: ObservableObject {

    /// The schema this build writes and the highest it can read.
    static let schemaVersion = 1

    @Published private(set) var items: [CaptureItem] = []
    /// A problem the screens must show rather than hide.
    @Published private(set) var loadError: String?
    /// True while the on-disk inbox could not be read and has not been resolved.
    /// Writes are refused in this state, because writing would replace a file
    /// that may still be intact on disk with an almost-empty one.
    @Published private(set) var isReadOnly = false
    /// Where an unreadable file was moved, so Settings/Inbox can point at it.
    @Published private(set) var quarantinePath: String?

    private let directory: URL
    private let itemsURL: URL
    private let queue = DispatchQueue(label: "vantage.capture.store")

    init(directory: URL? = nil) {
        let base = directory ?? Self.defaultDirectory()
        self.directory = base
        self.itemsURL = base.appendingPathComponent("capture.json")
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        reload()
    }

    static func defaultDirectory() -> URL {
        // An internal env seam for the integration harnesses, the same kind
        // `APIClient` already has for the backend port: a render or a check can
        // point the store at a scratch directory without touching the person's
        // real inbox. It is not a user-facing setting.
        if let override = ProcessInfo.processInfo.environment["VANTAGE_SUPPORT_DIR"],
           !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        let support = FileManager.default.urls(for: .applicationSupportDirectory,
                                               in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSSearchPathForDirectoriesInDomains(
                .applicationSupportDirectory, .userDomainMask, true).first
                   ?? (NSHomeDirectory() + "/Library/Application Support"))
        return support.appendingPathComponent("Vantage", isDirectory: true)
    }

    // MARK: - Reading

    struct File: Codable {
        var schema: Int
        var items: [CaptureItem]
    }

    func reload() {
        loadError = nil
        isReadOnly = false
        quarantinePath = nil
        guard let data = try? Data(contentsOf: itemsURL) else {
            // No file at all is a genuinely empty inbox, not a failure.
            items = []
            return
        }
        do {
            let file = try JSONDecoder.captureDecoder.decode(File.self, from: data)
            guard file.schema <= Self.schemaVersion else {
                throw StoreError.futureSchema(file.schema)
            }
            items = file.items.sorted { $0.createdAt > $1.createdAt }
        } catch {
            // Never overwrite. Move aside so the data is still recoverable by
            // hand, and refuse writes until the person has dealt with it.
            quarantine(because: error)
        }
    }

    private func quarantine(because error: Error) {
        let stamp = ISO8601DateFormatter().string(from: Date())
            .replacingOccurrences(of: ":", with: "-")
        let moved = itemsURL.deletingPathExtension()
            .appendingPathExtension("unreadable-\(stamp).json")
        do {
            try FileManager.default.moveItem(at: itemsURL, to: moved)
            quarantinePath = moved.path
        } catch {
            // The move itself failed (permission, a locked file). Still refuse
            // writes: the original file is untouched, which is the point.
            quarantinePath = itemsURL.path
        }
        items = []
        isReadOnly = true
        loadError = "Your capture inbox could not be read (\(error.localizedDescription)). "
            + "It was kept as \(((quarantinePath ?? itemsURL.path) as NSString).lastPathComponent) "
            + "and will not be overwritten. New captures are disabled until this is resolved."
    }

    enum StoreError: LocalizedError {
        case futureSchema(Int)
        case emptyContent
        case duplicate(String)
        case readOnly
        case writeFailed(String)

        var errorDescription: String? {
            switch self {
            case .futureSchema(let v):
                return "This inbox was written by a newer Vantage (schema \(v), this build "
                    + "understands \(CaptureStore.schemaVersion))."
            case .emptyContent:
                return "There is nothing to capture yet."
            case .duplicate(let where_):
                return "That is already in your inbox as “\(where_)”. Nothing was added."
            case .readOnly:
                return "Your inbox could not be read, so nothing new is being written to it. "
                    + "Resolve the unreadable file first (see below)."
            case .writeFailed(let detail):
                return "Saving failed: \(detail)"
            }
        }
    }

    // MARK: - Writing

    /// Add one item. Refuses empty content and a duplicate of an open item.
    ///
    /// The duplicate rule is what stops "Remember to call Sam" entered twice
    /// from looking like two things to do. An archived copy does not block a new
    /// capture — re-capturing something you archived is a deliberate act.
    @discardableResult
    func add(_ item: CaptureItem) -> Result<CaptureItem, Error> {
        guard !isReadOnly else { return .failure(StoreError.readOnly) }
        let content = item.content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !content.isEmpty else { return .failure(StoreError.emptyContent) }

        if let existing = items.first(where: {
            $0.isOpen && $0.fingerprint == item.fingerprint
        }) {
            return .failure(StoreError.duplicate(existing.headline))
        }

        var stored = item
        stored.content = content
        // A capture is the same act whichever surface made it; the caller may
        // not have set these.
        stored.createdAt = Date()
        stored.modifiedAt = stored.createdAt
        items.insert(stored, at: 0)
        switch persist() {
        case .success:
            return .success(stored)
        case .failure(let error):
            items.removeAll { $0.id == stored.id }
            return .failure(error)
        }
    }

    @discardableResult
    func update(_ item: CaptureItem) -> Result<Void, Error> {
        guard !isReadOnly else { return .failure(StoreError.readOnly) }
        guard let index = items.firstIndex(where: { $0.id == item.id }) else {
            return .failure(StoreError.writeFailed("that item is no longer in the inbox."))
        }
        var updated = item
        updated.content = updated.content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !updated.content.isEmpty else { return .failure(StoreError.emptyContent) }
        updated.modifiedAt = Date()
        let previous = items[index]
        items[index] = updated
        switch persist() {
        case .success: return .success(())
        case .failure(let error):
            items[index] = previous
            return .failure(error)
        }
    }

    /// Move an item to a new status.
    @discardableResult
    func setStatus(_ status: CaptureItem.Status, for id: UUID) -> Result<Void, Error> {
        guard var item = items.first(where: { $0.id == id }) else {
            return .failure(StoreError.writeFailed("that item is no longer in the inbox."))
        }
        item.status = status
        return update(item)
    }

    /// Record that an item became something else. Called only after the
    /// destination actually confirmed creation — the reference is the
    /// destination's own id, so the link can be checked.
    @discardableResult
    func markConverted(_ id: UUID, destination: CaptureDestination,
                       reference: String, label: String) -> Result<Void, Error> {
        guard var item = items.first(where: { $0.id == id }) else {
            return .failure(StoreError.writeFailed("that item is no longer in the inbox."))
        }
        item.status = .converted
        item.destinationReference = "\(destination.rawValue):\(reference)"
        item.destinationLabel = "\(destination.title) — \(label)"
        return update(item)
    }

    @discardableResult
    func delete(_ id: UUID) -> Result<Void, Error> {
        guard !isReadOnly else { return .failure(StoreError.readOnly) }
        guard let index = items.firstIndex(where: { $0.id == id }) else {
            return .success(())
        }
        let previous = items.remove(at: index)
        switch persist() {
        case .success: return .success(())
        case .failure(let error):
            items.insert(previous, at: index)
            return .failure(error)
        }
    }

    private func persist() -> Result<Void, Error> {
        do {
            let file = File(schema: Self.schemaVersion, items: items)
            try writeAtomically(try JSONEncoder.captureEncoder.encode(file), to: itemsURL)
            return .success(())
        } catch {
            loadError = StoreError.writeFailed(error.localizedDescription).errorDescription
            return .failure(StoreError.writeFailed(error.localizedDescription))
        }
    }

    /// Write beside the target and rename, so a crash never leaves a truncated file.
    private func writeAtomically(_ data: Data, to url: URL) throws {
        let tmp = url.appendingPathExtension("tmp")
        try data.write(to: tmp, options: .atomic)
        _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
    }

    // MARK: - Lookup

    /// Items in the inbox that need a person (unreviewed).
    var unreviewed: [CaptureItem] {
        items.filter { $0.status == .inbox }
    }

    var active: [CaptureItem] {
        items.filter { $0.isOpen }
    }

    func item(_ id: UUID) -> CaptureItem? {
        items.first { $0.id == id }
    }

    func search(_ query: String) -> [CaptureItem] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty else { return items }
        return items.filter {
            $0.content.lowercased().contains(needle)
                || $0.tags.contains { $0.lowercased().contains(needle) }
        }
    }
}

extension JSONEncoder {
    static var captureEncoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}

extension JSONDecoder {
    static var captureDecoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}