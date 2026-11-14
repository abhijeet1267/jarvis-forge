import Foundation

/// One step of a flow: a typed action plus the arguments it will run with.
///
/// A step stores an *action identifier and validated arguments*, never code. The
/// executor looks the id up in `MacActionKind`, so a hand-edited or corrupted
/// file cannot introduce an operation the app does not implement: the unknown id
/// fails validation and is reported, rather than being passed anywhere.
struct FlowStep: Codable, Identifiable, Equatable {
    let id: UUID
    var actionID: String
    var arguments: StoredArguments
    /// Overrides the flow's failure policy for this step when true: a failed
    /// step does not stop the run. Only meaningful for steps whose doc says the
    /// result is safe to continue past (a read that failed, a folder that was
    /// already open).
    var continueOnFailure: Bool

    init(id: UUID = UUID(), actionID: String,
         arguments: StoredArguments = StoredArguments(), continueOnFailure: Bool = false) {
        self.id = id
        self.actionID = actionID
        self.arguments = arguments
        self.continueOnFailure = continueOnFailure
    }

    /// The typed kind, or nil when the id is not an action this build implements.
    var kind: MacActionKind? { MacActionKind(rawValue: actionID) }

    /// A title for the editor and the preview, from the registry rather than
    /// from a stored string — so renaming a capability renames every flow.
    var title: String { kind?.displayName ?? "Unknown action (\(actionID))" }

    /// A one-line description of what this step will do, built from its args.
    var summary: String {
        let a = arguments
        switch kind {
        case .openApplication, .activateApplication, .quitApplication:
            return a.application.isEmpty ? "(no application named)" : a.application
        case .openFolder, .openFile:
            return a.path.isEmpty ? "(no path)" : a.path
        case .openURL:
            return a.url.isEmpty ? "(no address)" : a.url
        case .setVolume:
            return a.percent.map { "\($0)%" } ?? "(no level)"
        case .setMuted:
            return (a.flag ?? true) ? "mute" : "unmute"
        case .arrangeWindow:
            return (a.side ?? .left).displayName
        case .minimizeWindow, .closeWindow, .listWindows, .focusWindow:
            return a.application.isEmpty ? "frontmost window" : a.application
        case .openSystemSettings:
            return a.settingsPane.isEmpty ? "System Settings" : a.settingsPane
        case .listRunningApps, .frontmostApp, .systemInfo, .getVolume:
            return ""
        case .googleAgenda:
            return "today"
        case .googleOverdueTasks:
            return "overdue and upcoming"
        case .googleCreateTask:
            let title = a.live.googleTitle.isEmpty ? "(no title)" : a.live.googleTitle
            return "“\(title)”"
        case .googleCreateEvent:
            let title = a.title.isEmpty ? "(no title)" : a.title
            let start = a.startDate.isEmpty ? "(no start time)" : a.startDate
            return "“\(title)” at \(start)"
        case .openToday:
            return ""
        case .readClipboard, .displays, .audioDevices, .currentTime:
            return ""
        case .writeClipboard:
            return a.text.isEmpty ? "(no text)" : "“\(a.text.prefix(40))”"
        case .postNotification:
            let title = a.notificationTitle.isEmpty ? "Vantage" : a.notificationTitle
            return "“\(title)”"
        case .moveFile:
            return a.path.isEmpty ? "(no file)" : "\(a.path) → \(a.destination)"
        case .renameFile:
            return a.path.isEmpty ? "(no file)" : "→ \(a.newName)"
        case .deleteFile:
            return a.path.isEmpty ? "(no file)" : "trash \(a.path)"
        case nil:
            return "this action is not available in this build"
        }
    }
}

/// The persistable form of `MacActionArguments`.
///
/// A separate Codable struct rather than encoding the live type, because what is
/// saved must stay a *narrow, versioned* surface: adding a field to the executor's
/// argument type must not silently start persisting it. Optional fields default so
/// an older file still decodes.
struct StoredArguments: Codable, Equatable {
    var application: String = ""
    var windowTitle: String = ""
    var url: String = ""
    var path: String = ""
    var text: String = ""
    var percent: Int? = nil
    var flag: Bool? = nil
    var side: MacWindowSide? = nil
    var settingsPane: String = ""

    // Google. Optional/defaulted like every other field, so a flow file written
    // by an older build still decodes.
    var title: String = ""
    var startDate: String = ""
    var durationMinutes: Int? = nil
    var location: String = ""
    var taskListID: String = ""

    // Clipboard, notifications, and file operations.
    var notificationTitle: String = ""
    var newName: String = ""
    var destination: String = ""

    init() {}

    init(_ live: MacActionArguments) {
        application = live.application
        windowTitle = live.windowTitle
        url = live.url
        path = live.path
        text = live.text
        percent = live.percent
        flag = live.flag
        side = live.side
        settingsPane = live.settingsPane
        title = live.title
        startDate = live.startDate
        durationMinutes = live.durationMinutes
        location = live.location
        taskListID = live.taskListID
        notificationTitle = live.notificationTitle
        newName = live.newName
        destination = live.destination
    }

    var live: MacActionArguments {
        var a = MacActionArguments()
        a.application = application
        a.windowTitle = windowTitle
        a.url = url
        a.path = path
        a.text = text
        a.percent = percent
        a.flag = flag
        a.side = side
        a.settingsPane = settingsPane
        a.title = title
        a.startDate = startDate
        a.durationMinutes = durationMinutes
        a.location = location
        a.taskListID = taskListID
        a.notificationTitle = notificationTitle
        a.newName = newName
        a.destination = destination
        return a
    }
}

/// What happens when a step fails.
enum FlowFailurePolicy: String, Codable, CaseIterable {
    /// The default. A failed step stops the run; later steps do not run.
    case stop
    /// Keep going only for steps explicitly marked `continueOnFailure`.
    case continueIndependent

    var displayName: String {
        switch self {
        case .stop: return "Stop at the first failure"
        case .continueIndependent: return "Continue past steps marked safe"
        }
    }
}

/// A saved, versioned workflow.
struct Flow: Codable, Identifiable, Equatable {
    /// The schema version. Bumped when a change would make an older build
    /// misread a file; `FlowStore` refuses a file from the future rather than
    /// guessing at it.
    static let schemaVersion = 1

    let id: UUID
    var name: String
    var description: String
    var steps: [FlowStep]
    var failurePolicy: FlowFailurePolicy
    var createdAt: Date
    var updatedAt: Date
    var schema: Int

    init(id: UUID = UUID(), name: String, description: String = "",
         steps: [FlowStep] = [], failurePolicy: FlowFailurePolicy = .stop,
         createdAt: Date = Date(), updatedAt: Date = Date(),
         schema: Int = Flow.schemaVersion) {
        self.id = id
        self.name = name
        self.description = description
        self.steps = steps
        self.failurePolicy = failurePolicy
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.schema = schema
    }

    /// Problems that make this flow unrunnable. Empty means it is valid.
    ///
    /// Validation is deliberately *structural and local*: it answers "can this
    /// flow even be attempted". Whether the current process has Accessibility,
    /// whether an app is installed, and whether a step needs approval are
    /// answered by the preflight, because those change between observations and
    /// saving.
    func validate() -> [FlowProblem] {
        var problems: [FlowProblem] = []
        if name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            problems.append(.init(stepID: nil, message: "The flow needs a name."))
        }
        if steps.isEmpty {
            problems.append(.init(stepID: nil, message: "The flow has no steps."))
        }
        for step in steps {
            guard let kind = step.kind else {
                problems.append(.init(stepID: step.id,
                                      message: "“\(step.actionID)” is not an action this build supports."))
                continue
            }
            guard let policy = MacActionPolicy.forKind(kind) else {
                problems.append(.init(stepID: step.id, message: "\(kind.displayName) has no policy."))
                continue
            }
            let args = step.arguments.live
            for keyPath in policy.required
            where args[keyPath: keyPath].trimmingCharacters(in: .whitespaces).isEmpty {
                problems.append(.init(stepID: step.id,
                                      message: "\(kind.displayName) is missing its \(Self.fieldName(keyPath))."))
            }
            // Content checks reuse the router, so a saved flow cannot hold a URL
            // or volume that the executor would refuse at run time.
            //
            // A Google *write* is checked for structure only: whether the
            // integration is connected is asked again at run time (a saved
            // workflow is not permanent authorization — the connection can be
            // revoked between saving and running), so a flow saved while
            // disconnected must remain saveable and explain itself when run.
            let router = MacActionRouter(permissions: { MacPermissionStatus(accessibilityGranted: true) })
            if Self.isGoogleWrite(kind) {
                if let problem = Self.googleWriteProblem(kind: kind, args: args) {
                    problems.append(.init(stepID: step.id, message: problem))
                }
            } else if let refusal = Self.contentRefusal(router: router, kind: kind, arguments: args) {
                problems.append(.init(stepID: step.id,
                                      message: "\(kind.displayName): \(refusal)"))
            }
        }
        return problems
    }

    /// Google writes are validated structurally at save time.
    private static func isGoogleWrite(_ kind: MacActionKind) -> Bool {
        kind == .googleCreateTask || kind == .googleCreateEvent
    }

    private static func googleWriteProblem(kind: MacActionKind, args: MacActionArguments) -> String? {
        switch kind {
        case .googleCreateTask:
            let title = args.googleTitle.trimmingCharacters(in: .whitespacesAndNewlines)
            if title.isEmpty { return "\(kind.displayName) is missing its title." }
        case .googleCreateEvent:
            let title = args.title.trimmingCharacters(in: .whitespacesAndNewlines)
            if title.isEmpty { return "\(kind.displayName) is missing its title." }
            let start = args.startDate.trimmingCharacters(in: .whitespacesAndNewlines)
            if start.isEmpty { return "\(kind.displayName) is missing its start time." }
            if CalendarProposal.parseTime(start) == nil
                && TodayMapping.parseInstant(start) == nil {
                return "\(kind.displayName): \(start) is not a date and time Vantage can read."
            }
        default:
            return nil
        }
        return nil
    }

    /// Only the argument-content rules, without permission or caller checks.
    private static func contentRefusal(router: MacActionRouter, kind: MacActionKind,
                                       arguments: MacActionArguments) -> String? {
        do {
            _ = try router.validate(kind: kind, arguments: arguments)
            return nil
        } catch let refusal as MacControlRefusal {
            return refusal.message
        } catch {
            return error.localizedDescription
        }
    }

    private static func fieldName(_ keyPath: KeyPath<MacActionArguments, String>) -> String {
        switch keyPath {
        case \MacActionArguments.application: return "application"
        case \MacActionArguments.url: return "web address"
        case \MacActionArguments.path: return "path"
        case \MacActionArguments.windowTitle: return "window title"
        case \MacActionArguments.text: return "text"
        case \MacActionArguments.title: return "title"
        case \MacActionArguments.startDate: return "start time"
        default: return "argument"
        }
    }

    struct FlowProblem: Equatable {
        let stepID: UUID?
        let message: String
    }
}