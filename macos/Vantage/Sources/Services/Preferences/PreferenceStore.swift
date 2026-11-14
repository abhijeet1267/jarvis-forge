import Foundation

/// The person's explicit preferences, on disk.
///
/// Same store conventions as the capture inbox and flows: one atomically-written
/// JSON file, schema-checked, and never overwritten when it cannot be read.
///
/// Two behaviours the brief asks for and this implements:
/// * **A conflict is shown, not resolved silently.** Two enabled preferences of
///   the same kind and key are a conflict; `conflicts` reports it and the screen
///   draws it. `effectiveValue` applies a defined rule (most recently modified
///   wins) *and* says that a rule was used, so nothing is hidden.
/// * **Preferences never override a current instruction.** Nothing in this store
///   executes anything; the consumers (document save path, flow draft defaults,
///   briefing order) ask for a *default* and are expected to use it only when the
///   person's current request did not name one.
final class PreferenceStore: ObservableObject {

    static let schemaVersion = 1

    @Published private(set) var preferences: [Preference] = []
    @Published private(set) var loadError: String?
    @Published private(set) var isReadOnly = false
    @Published private(set) var quarantinePath: String?

    private let itemsURL: URL

    init(directory: URL? = nil) {
        let base = directory ?? CaptureStore.defaultDirectory()
        self.itemsURL = base.appendingPathComponent("preferences.json")
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        reload()
    }

    struct File: Codable {
        var schema: Int
        var preferences: [Preference]
    }

    // MARK: - Reading

    func reload() {
        loadError = nil
        isReadOnly = false
        quarantinePath = nil
        guard let data = try? Data(contentsOf: itemsURL) else {
            preferences = []
            return
        }
        do {
            let file = try JSONDecoder.captureDecoder.decode(File.self, from: data)
            guard file.schema <= Self.schemaVersion else { throw StoreError.futureSchema(file.schema) }
            preferences = file.preferences.sorted { $0.modifiedAt > $1.modifiedAt }
        } catch {
            let stamp = ISO8601DateFormatter().string(from: Date())
                .replacingOccurrences(of: ":", with: "-")
            let moved = itemsURL.deletingPathExtension()
                .appendingPathExtension("unreadable-\(stamp).json")
            do {
                try FileManager.default.moveItem(at: itemsURL, to: moved)
                quarantinePath = moved.path
            } catch {
                quarantinePath = itemsURL.path
            }
            preferences = []
            isReadOnly = true
            loadError = "Your preferences could not be read (\(error.localizedDescription)). "
                + "They were kept and will not be overwritten. Adding is disabled until this "
                + "is resolved."
        }
    }

    enum StoreError: LocalizedError {
        case futureSchema(Int)
        case invalid(String)
        case readOnly
        case writeFailed(String)

        var errorDescription: String? {
            switch self {
            case .futureSchema(let v):
                return "These preferences were written by a newer Vantage (schema \(v))."
            case .invalid(let message): return message
            case .readOnly:
                return "Your preferences could not be read, so nothing new is being written."
            case .writeFailed(let detail): return "Saving failed: \(detail)"
            }
        }
    }

    // MARK: - Writing

    /// Add a preference, validating its value and refusing secrets.
    @discardableResult
    func add(_ preference: Preference) -> Result<Preference, Error> {
        guard !isReadOnly else { return .failure(StoreError.readOnly) }
        guard let problem = validationProblem(preference) else {
            var stored = preference
            stored.createdAt = Date()
            stored.modifiedAt = stored.createdAt
            stored.key = stored.key.trimmingCharacters(in: .whitespacesAndNewlines)
            preferences.insert(stored, at: 0)
            switch persist() {
            case .success: return .success(stored)
            case .failure(let error):
                preferences.removeAll { $0.id == stored.id }
                return .failure(error)
            }
        }
        return .failure(StoreError.invalid(problem))
    }

    @discardableResult
    func update(_ preference: Preference) -> Result<Void, Error> {
        guard !isReadOnly else { return .failure(StoreError.readOnly) }
        guard let problem = validationProblem(preference) else {
            guard let index = preferences.firstIndex(where: { $0.id == preference.id }) else {
                return .failure(StoreError.writeFailed("that preference no longer exists."))
            }
            var updated = preference
            updated.modifiedAt = Date()
            let previous = preferences[index]
            preferences[index] = updated
            switch persist() {
            case .success: return .success(())
            case .failure(let error):
                preferences[index] = previous
                return .failure(error)
            }
        }
        return .failure(StoreError.invalid(problem))
    }

    @discardableResult
    func setEnabled(_ enabled: Bool, for id: UUID) -> Result<Void, Error> {
        guard var preference = preferences.first(where: { $0.id == id }) else {
            return .failure(StoreError.writeFailed("that preference no longer exists."))
        }
        preference.enabled = enabled
        return update(preference)
    }

    @discardableResult
    func delete(_ id: UUID) -> Result<Void, Error> {
        guard !isReadOnly else { return .failure(StoreError.readOnly) }
        guard let index = preferences.firstIndex(where: { $0.id == id }) else {
            return .success(())
        }
        let previous = preferences.remove(at: index)
        switch persist() {
        case .success: return .success(())
        case .failure(let error):
            preferences.insert(previous, at: index)
            return .failure(error)
        }
    }

    /// Remove everything. Asked for by "Restore safe defaults", with confirmation
    /// in the UI — this is destructive and has no undo.
    func removeAll() -> Result<Void, Error> {
        guard !isReadOnly else { return .failure(StoreError.readOnly) }
        let previous = preferences
        preferences = []
        switch persist() {
        case .success: return .success(())
        case .failure(let error):
            preferences = previous
            return .failure(error)
        }
    }

    private func validationProblem(_ preference: Preference) -> String? {
        if let refusal = Preference.secretRefusal(in: preference.value) { return refusal }
        if let refusal = Preference.secretRefusal(in: preference.note) { return refusal }
        if preference.normalizedKey.isEmpty { return "A preference needs a name (its key)." }
        return preference.kind.validate(preference.value)
    }

    private func persist() -> Result<Void, Error> {
        do {
            let file = File(schema: Self.schemaVersion, preferences: preferences)
            let tmp = itemsURL.appendingPathExtension("tmp")
            try JSONEncoder.captureEncoder.encode(file).write(to: tmp, options: .atomic)
            _ = try FileManager.default.replaceItemAt(itemsURL, withItemAt: tmp)
            return .success(())
        } catch {
            loadError = StoreError.writeFailed(error.localizedDescription).errorDescription
            return .failure(StoreError.writeFailed(error.localizedDescription))
        }
    }

    // MARK: - Use

    /// The enabled preferences of one kind, in most-recently-modified order.
    func enabled(_ kind: Preference.Kind) -> [Preference] {
        preferences.filter { $0.kind == kind && $0.enabled }
            .sorted { $0.modifiedAt > $1.modifiedAt }
    }

    /// The value a consumer should use for `kind`/`key`, and whether a rule
    /// decided it.
    ///
    /// A consumer must call this only when the person's **current** request did
    /// not name a value; that rule lives at the use site, not here, because only
    /// the use site knows what was asked for this time.
    ///
    /// When two enabled preferences have the same key, the most recently modified
    /// wins *and* `conflict` is true, so the UI can show which rule applied
    /// instead of pretending there is one preference.
    func effectiveValue(kind: Preference.Kind, key: String)
        -> (value: String?, conflict: Bool, winner: Preference?) {
        let matches = enabled(kind).filter { $0.normalizedKey == key.lowercased() }
        guard let winner = matches.first else { return (nil, false, nil) }
        return (winner.value, matches.count > 1, winner)
    }

    /// Every (kind, key) with more than one enabled preference.
    var conflicts: [(kind: Preference.Kind, key: String, preferences: [Preference])] {
        var grouped: [String: [Preference]] = [:]
        for preference in preferences where preference.enabled {
            grouped["\(preference.kind.rawValue)|\(preference.normalizedKey)", default: []]
                .append(preference)
        }
        return grouped.compactMap { _, list in
            guard list.count > 1, let first = list.first else { return nil }
            return (first.kind, first.key, list)
        }
        .sorted { $0.key < $1.key }
    }

    /// Where to save a generated summary, applying a folder preference only when
    /// the person did not choose one for *this* save.
    ///
    /// Returns nil when there is no preference, so the caller falls back to the
    /// file picker rather than silently writing somewhere surprising.
    func preferredSummaryFolder() -> URL? {
        guard let preference = enabled(.preferredFolder)
            .first(where: { $0.normalizedKey == "summary" || $0.normalizedKey == "summaries" })
        else { return nil }
        guard let url = try? MacPathGuard.resolve(preference.value),
              MacPathGuard.refusal(for: url) == nil,
              FileManager.default.fileExists(atPath: url.path) else { return nil }
        return url
    }

    /// The file name for a generated summary, from the naming preference.
    ///
    /// `{name}` is the source document's own name; `{date}` is today. Substitution
    /// only — a pattern cannot introduce a path separator (validated at entry) and
    /// this re-checks it so a hand-edited file cannot escape the folder.
    func generatedFileName(baseName: String, date: Date = Date()) -> String {
        let fallback = baseName + " summary"
        guard let preference = enabled(.fileNaming).first else { return fallback + ".md" }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        var name = preference.value
            .replacingOccurrences(of: "{name}", with: baseName)
            .replacingOccurrences(of: "{date}", with: formatter.string(from: date))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if name.isEmpty { name = fallback }
        name = name.replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ":", with: "-")
        return name.contains(".") ? name : name + ".md"
    }

    /// The default failure policy for a newly drafted flow.
    func defaultFlowPolicy() -> FlowFailurePolicy? {
        guard let preference = enabled(.defaultFlowPolicy).first else { return nil }
        return preference.value.lowercased() == "continue" ? .continueIndependent : .stop
    }

    /// The briefing sections in the person's preferred order.
    ///
    /// A preference only *reorders*: a section it names moves to the front, and
    /// nothing is removed. A preference must not be able to hide "Needs
    /// attention" by mistake.
    func briefingOrder() -> [String] {
        let preferred = enabled(.briefingSection).map { $0.value.lowercased() }
        let standard = ["schedule", "due", "overdue", "upcoming", "capture",
                        "failures", "approvals"]
        let front = preferred.filter { standard.contains($0) }
        return front + standard.filter { !front.contains($0) }
    }

    /// The preferred application for an activity ("browser", "editor").
    func preferredApplication(for activity: String) -> String? {
        effectiveValue(kind: .preferredApplication, key: activity).value
    }
}