import Foundation

/// Turns an inbox item into a real thing — only when a person presses the button.
///
/// This exists as its own type because the three destinations have three
/// different owners and three different failure modes:
///
/// * **Task** — the backend planner is the owner (`POST /planner/tasks`), so the
///   item records the returned `task_ref`. Vantage does not keep a second copy.
/// * **Reminder** — the backend scheduler owns it (`POST /schedules` with
///   `action: "reminder"`). A reminder that only exists in this app would not
///   fire when the app is closed.
/// * **Flow** — the *local* flow store owns it, but a conversion produces an
///   unsaved draft. Saving stays a separate, explicit act in the Flows editor.
///
/// Every method reports what actually happened. A converted item is marked
/// converted only after the destination confirmed it, so the inbox cannot claim
/// a task exists that the planner never received.
@MainActor
final class CaptureConversionService {

    /// The result of a conversion, for the screen to show.
    struct Outcome {
        let summary: String
        let succeeded: Bool
        /// A flow draft the person should review. Nil for task/reminder.
        var flowDraft: Flow?
        /// An error the screen must display as an error rather than a success.
        var error: String?
    }

    private let api: APIClient
    private let store: CaptureStore

    init(api: APIClient = .shared, store: CaptureStore? = nil) {
        self.api = api
        self.store = store ?? CaptureRuntime.shared.store
    }

    /// Convert an item into a planner task.
    ///
    /// `title` and `due` are the person's edits in the conversion sheet, not the
    /// raw capture — the brief requires a reviewable draft before anything is
    /// added to the planner.
    func convertToTask(_ item: CaptureItem, title: String, detail: String,
                       priority: String, due: Date?) async -> Outcome {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return Outcome(summary: "", succeeded: false,
                           error: "A task needs a title. Nothing was created.")
        }
        do {
            let task = try await api.createPlannerTask(
                title: trimmed, priority: priority, detail: detail, dueAt: due)
            let ref = task.task_ref
            switch store.markConverted(item.id, destination: .task, reference: ref, label: trimmed) {
            case .success:
                return Outcome(summary: "Added “\(trimmed)” to the planner.", succeeded: true)
            case .failure(let error):
                return Outcome(summary: "", succeeded: false,
                               error: "The task was created (\(ref)) but your inbox could not "
                                 + "record it: \(error.localizedDescription)")
            }
        } catch {
            return Outcome(summary: "", succeeded: false,
                           error: "Could not create the task: \(error.localizedDescription)")
        }
    }

    /// Convert an item into a reminder, owned by the backend scheduler.
    ///
    /// The reminder time is the person's choice in the sheet. The scheduler's
    /// grammar only expresses a clock time today or a short relative delay, so a
    /// date further out is refused *here* with a sentence explaining the
    /// limitation — see `APIClient.createReminder`. What this must never do is
    /// silently downgrade a "remind me Friday" into a job that fires every day.
    func convertToReminder(_ item: CaptureItem, message: String,
                           at date: Date) async -> Outcome {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return Outcome(summary: "", succeeded: false,
                           error: "A reminder needs a message. Nothing was created.")
        }
        do {
            let job = try await api.createReminder(message: trimmed, at: date)
            switch store.markConverted(item.id, destination: .reminder,
                                       reference: job.schedule_id, label: trimmed) {
            case .success:
                let when = date.formatted(date: .abbreviated, time: .shortened)
                return Outcome(summary: "Reminder set for \(when).", succeeded: true)
            case .failure(let error):
                return Outcome(summary: "", succeeded: false,
                               error: "The reminder was created but your inbox could not record "
                                 + "it: \(error.localizedDescription)")
            }
        } catch {
            return Outcome(summary: "", succeeded: false,
                           error: "Could not create the reminder: \(error.localizedDescription)")
        }
    }

    /// Draft a flow from the item's words. Nothing is saved or run.
    ///
    /// The draft comes from the *same* authoring path the palette and Flows
    /// screen use (`FlowAuthoring`), so an item cannot produce a flow a typed
    /// sentence could not. When no clause maps to an action, an empty draft is
    /// returned with the problem named, rather than a flow of nothing.
    func convertToFlow(_ item: CaptureItem, applications: [String]) -> Outcome {
        let authoring = FlowAuthoring(applications: applications)
        let name = Self.flowName(for: item)

        switch authoring.interpret("create a \(name) flow that \(item.content)", store: .shared) {
        case .draft(let flow), .incompleteDraft(let flow, _):
            guard !flow.steps.isEmpty else {
                return Outcome(summary: "", succeeded: false,
                               flowDraft: nil,
                               error: "Nothing in “\(item.content)” matches an action Vantage can "
                                 + "perform, so there is no draft to review. You can build the flow "
                                 + "by hand in Flows.")
            }
            // Not marked converted here: the draft is unsaved, so the inbox item
            // stays actionable until the person actually saves the flow.
            return Outcome(summary: "Drafted \(flow.steps.count) steps. Review and save it in Flows — "
                             + "nothing has run.",
                           succeeded: true, flowDraft: flow)
        case .run(let flow):
            return Outcome(summary: "", succeeded: false,
                           flowDraft: nil,
                           error: "That reads as “run \(flow.name)”, not a new flow.")
        case .describe(let flow):
            return Outcome(summary: "", succeeded: false,
                           flowDraft: nil,
                           error: "That describes “\(flow.name)” rather than drafting a new flow.")
        case .unknownFlow(let message):
            return Outcome(summary: "", succeeded: false, flowDraft: nil, error: message)
        case .notAFlowRequest:
            return Outcome(summary: "", succeeded: false,
                           flowDraft: nil,
                           error: "That does not describe an action Vantage can put in a flow.")
        }
    }

    /// Mark a flow draft saved. Called from the Flows screen once the person
    /// presses Save, so the link is recorded only when the flow really exists.
    func markFlowSaved(itemID: UUID, flow: Flow) -> String? {
        switch store.markConverted(itemID, destination: .flow,
                                   reference: flow.id.uuidString, label: flow.name) {
        case .success: return nil
        case .failure(let error): return error.localizedDescription
        }
    }

    /// A short, readable flow name from the captured sentence.
    private static func flowName(for item: CaptureItem) -> String {
        let words = item.headline
            .split(separator: " ")
            .prefix(6)
            .map(String.init)
        let base = words.joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: " .,;:"))
        return base.isEmpty ? "Captured Flow" : base
    }
}