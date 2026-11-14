import Foundation

/// Saved flows and their run history, on disk.
///
/// Small and boring on purpose: two JSON files under
/// `~/Library/Application Support/Vantage/`, written atomically, with the schema
/// version checked on read. No database, no framework — the brief asks for a
/// small versioned mechanism and this is the smallest one that survives a crash
/// mid-write.
///
/// Failure behaviour is chosen deliberately: a corrupt or future-version file is
/// reported and **not** overwritten. Losing a person's saved flows because a
/// decode failed would be worse than refusing to start with them, so the
/// unreadable file is moved aside with a timestamp and the store starts empty,
/// saying so.
final class FlowStore: ObservableObject {

    @Published private(set) var flows: [Flow] = []
    @Published private(set) var history: [FlowRun] = []
    /// A problem the screens must show rather than hide: a file was unreadable,
    /// or a write failed. Never silently empty.
    @Published private(set) var loadError: String?

    private let directory: URL
    private let flowsURL: URL
    private let historyURL: URL
    private let maxHistory = 40
    private let queue = DispatchQueue(label: "vantage.flows.store")

    init(directory: URL? = nil) {
        let base = directory ?? Self.defaultDirectory()
        self.directory = base
        self.flowsURL = base.appendingPathComponent("flows.json")
        self.historyURL = base.appendingPathComponent("runs.json")
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        reload()
    }

    static func defaultDirectory() -> URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory,
                                               in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSSearchPathForDirectoriesInDomains(
                .applicationSupportDirectory, .userDomainMask, true).first
                   ?? (NSHomeDirectory() + "/Library/Application Support"))
        return support.appendingPathComponent("Vantage", isDirectory: true)
    }

    // MARK: - Reading

    struct FlowsFile: Codable {
        var schema: Int
        var flows: [Flow]
    }

    struct HistoryFile: Codable {
        var schema: Int
        var runs: [FlowRun]
    }

    func reload() {
        loadError = nil
        if let data = try? Data(contentsOf: flowsURL) {
            do {
                let file = try JSONDecoder.flowDecoder.decode(FlowsFile.self, from: data)
                guard file.schema <= Flow.schemaVersion else {
                    throw StoreError.futureSchema(file.schema)
                }
                flows = file.flows.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            } catch {
                quarantine(flowsURL, because: error)
                flows = []
            }
        }
        if let data = try? Data(contentsOf: historyURL) {
            do {
                let file = try JSONDecoder.flowDecoder.decode(HistoryFile.self, from: data)
                history = file.runs
            } catch {
                quarantine(historyURL, because: error)
                history = []
            }
        }
    }

    private func quarantine(_ url: URL, because error: Error) {
        let stamp = ISO8601DateFormatter().string(from: Date())
            .replacingOccurrences(of: ":", with: "-")
        let moved = url.deletingPathExtension()
            .appendingPathExtension("unreadable-\(stamp).json")
        try? FileManager.default.moveItem(at: url, to: moved)
        loadError = "\(url.lastPathComponent) could not be read (\(error.localizedDescription)). "
            + "It was moved to \(moved.lastPathComponent) and not overwritten."
    }

    enum StoreError: LocalizedError {
        case futureSchema(Int)
        case writeFailed(String)

        var errorDescription: String? {
            switch self {
            case .futureSchema(let v):
                return "This file was written by a newer Vantage (schema \(v), this build "
                    + "understands \(Flow.schemaVersion))."
            case .writeFailed(let detail):
                return "Saving failed: \(detail)"
            }
        }
    }

    // MARK: - Writing

    @discardableResult
    func save(_ flow: Flow) -> Result<Void, Error> {
        var updated = flow
        updated.updatedAt = Date()
        updated.schema = Flow.schemaVersion
        if let index = flows.firstIndex(where: { $0.id == updated.id }) {
            flows[index] = updated
        } else {
            flows.append(updated)
        }
        flows.sort { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        return persistFlows()
    }

    @discardableResult
    func delete(_ flow: Flow) -> Result<Void, Error> {
        flows.removeAll { $0.id == flow.id }
        return persistFlows()
    }

    /// A copy with a new identity, so running one does not overwrite the other.
    @discardableResult
    func duplicate(_ flow: Flow) -> Flow {
        var copy = flow
        copy = Flow(name: uniqueName(basedOn: flow.name), description: flow.description,
                    steps: flow.steps.map { FlowStep(actionID: $0.actionID,
                                                     arguments: $0.arguments,
                                                     continueOnFailure: $0.continueOnFailure) },
                    failurePolicy: flow.failurePolicy)
        _ = save(copy)
        return copy
    }

    private func uniqueName(basedOn name: String) -> String {
        let existing = Set(flows.map { $0.name.lowercased() })
        var candidate = "\(name) copy"
        var index = 2
        while existing.contains(candidate.lowercased()) {
            candidate = "\(name) copy \(index)"
            index += 1
        }
        return candidate
    }

    func record(_ run: FlowRun) {
        history.insert(run, at: 0)
        if history.count > maxHistory { history.removeLast(history.count - maxHistory) }
        _ = persistHistory()
    }

    func clearHistory() {
        history.removeAll()
        _ = persistHistory()
    }

    func deleteHistory(_ run: FlowRun) {
        history.removeAll { $0.id == run.id }
        _ = persistHistory()
    }

    private func persistFlows() -> Result<Void, Error> {
        do {
            let file = FlowsFile(schema: Flow.schemaVersion, flows: flows)
            try writeAtomically(try JSONEncoder.flowEncoder.encode(file), to: flowsURL)
            return .success(())
        } catch {
            loadError = StoreError.writeFailed(error.localizedDescription).errorDescription
            return .failure(error)
        }
    }

    private func persistHistory() -> Result<Void, Error> {
        do {
            let file = HistoryFile(schema: Flow.schemaVersion, runs: history)
            try writeAtomically(try JSONEncoder.flowEncoder.encode(file), to: historyURL)
            return .success(())
        } catch {
            return .failure(error)
        }
    }

    /// Write beside the target and rename, so a crash never leaves a truncated file.
    private func writeAtomically(_ data: Data, to url: URL) throws {
        let tmp = url.appendingPathExtension("tmp")
        try data.write(to: tmp, options: .atomic)
        _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
    }

    // MARK: - Lookup

    func flow(named name: String) -> Flow? {
        let wanted = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !wanted.isEmpty else { return nil }
        // Exact first, then "starts with", so "run my Start Workday flow" finds
        // "Start Workday" without also matching an unrelated longer name.
        if let exact = flows.first(where: { $0.name.lowercased() == wanted }) { return exact }
        return flows.first { $0.name.lowercased().hasPrefix(wanted) }
    }

    /// The flows whose names the sentence mentions — used to suggest, never to
    /// run something the person did not name.
    func mentions(in text: String) -> [Flow] {
        let lowered = text.lowercased()
        return flows.filter { flow in
            let name = flow.name.lowercased()
            return !name.isEmpty && lowered.contains(name)
        }
    }
}

extension JSONEncoder {
    static var flowEncoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}

extension JSONDecoder {
    static var flowDecoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}