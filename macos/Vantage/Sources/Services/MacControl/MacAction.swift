import Foundation

/// The finite set of operations Vantage may perform on this Mac.
///
/// This enum *is* the security boundary. Nothing else can reach the executor: a
/// natural-language model, the backend, or a LAN client can only produce one of
/// these cases, and there is no case that carries a shell command, an
/// AppleScript source, or a free-form path to run. Adding a capability means
/// adding a case here, a policy row in `MacActionPolicy`, and an explicit
/// `switch` arm in `MacControlService` — three places that each refuse
/// unknown input by construction.
enum MacActionKind: String, Codable, CaseIterable {
    // Application control
    case listRunningApps       = "list_running_apps"
    case frontmostApp          = "frontmost_app"
    case openApplication       = "open_application"
    case activateApplication   = "activate_application"
    case openURL               = "open_url"
    case openFile              = "open_file"
    case openFolder            = "open_folder"
    case quitApplication       = "quit_application"

    // Window control (Accessibility)
    case listWindows           = "list_windows"
    case minimizeWindow        = "minimize_window"
    case closeWindow           = "close_window"
    case arrangeWindow         = "arrange_window"
    case focusWindow           = "focus_window"

    // System control
    case systemInfo            = "system_info"
    case getVolume             = "get_volume"
    case setVolume             = "set_volume"
    case setMuted              = "set_muted"
    case openSystemSettings    = "open_system_settings"

    // Google (through the backend, which owns the OAuth token)
    /// Reads the calendar for today and reports the agenda. A read: it can
    /// fail without changing anything, and that failure is reported.
    case googleAgenda          = "google_agenda"
    /// Reads overdue/upcoming Google tasks.
    case googleOverdueTasks    = "google_overdue_tasks"
    /// Creates a Google task. A remote write, so it needs approval.
    case googleCreateTask      = "google_create_task"
    /// Creates a calendar event. A remote write, so it needs approval.
    case googleCreateEvent     = "google_create_event"
    /// Brings the Today workspace forward. Navigation, not a change.
    case openToday             = "open_today"

    // Clipboard (read/write is Class B: reversible, no file changes)
    case readClipboard         = "read_clipboard"
    case writeClipboard        = "write_clipboard"

    // Notifications
    case postNotification      = "post_notification"

    // Files — the only file *mutations* the native layer performs. Each is
    // confirmed, and deletion refuses a directory outright.
    case moveFile              = "move_file"
    case renameFile            = "rename_file"
    case deleteFile            = "delete_file"

    // Read-only system detail the brief asks for
    case displays              = "list_displays"
    case audioDevices          = "list_audio_devices"
    case currentTime           = "current_time"

    /// A published, human-readable identifier. Kept stable because it is what
    /// the history and the tests refer to; the Swift case name may be refactored.
    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .listRunningApps: return "List running applications"
        case .frontmostApp: return "Foreground application"
        case .openApplication: return "Open application"
        case .activateApplication: return "Switch to application"
        case .openURL: return "Open website"
        case .openFile: return "Open file"
        case .openFolder: return "Open folder"
        case .quitApplication: return "Quit application"
        case .listWindows: return "List windows"
        case .minimizeWindow: return "Minimize window"
        case .closeWindow: return "Close window"
        case .arrangeWindow: return "Arrange window"
        case .focusWindow: return "Focus window"
        case .systemInfo: return "System and display information"
        case .getVolume: return "Read volume"
        case .setVolume: return "Set volume"
        case .setMuted: return "Mute or unmute"
        case .openSystemSettings: return "Open a System Settings page"
        case .googleAgenda: return "Show today's Google Calendar agenda"
        case .googleOverdueTasks: return "Show overdue Google Tasks"
        case .googleCreateTask: return "Create a Google task"
        case .googleCreateEvent: return "Create a Google Calendar event"
        case .openToday: return "Open Today"
        case .readClipboard: return "Read the clipboard"
        case .writeClipboard: return "Copy text to the clipboard"
        case .postNotification: return "Show a notification"
        case .moveFile: return "Move a file"
        case .renameFile: return "Rename a file"
        case .deleteFile: return "Delete a file"
        case .displays: return "List displays"
        case .audioDevices: return "List audio devices"
        case .currentTime: return "Read the date and time"
        }
    }
}

/// The typed arguments an action may carry.
///
/// A struct with a closed set of fields rather than `[String: Any]`: the
/// executor reads named properties, so an argument it does not understand
/// cannot reach a native API by accident. Each action declares which fields it
/// needs in `MacActionPolicy.requiredArguments`; `MacActionRouter` refuses a
/// call whose required field is empty before any validation of *contents*
/// happens.
struct MacActionArguments: Equatable {
    /// An application name or bundle identifier as the person typed it.
    var application: String = ""
    /// A window title fragment, used to pick one window when several match.
    var windowTitle: String = ""
    /// A URL string. Validated to http/https only — never a `file:` or
    /// custom-scheme URL, which is how "open a website" becomes "launch an
    /// arbitrary handler".
    var url: String = ""
    /// An absolute filesystem path for open/reveal-style actions.
    var path: String = ""
    /// A destination path for create/copy/move. Always resolved and checked
    /// against `MacPathGuard` before use.
    var destination: String = ""
    /// Free text for find/rename. Never executed; only compared or written.
    var text: String = ""
    /// A whole-number percentage 0–100 for volume.
    var percent: Int? = nil
    /// A boolean target (mute on/off, quit confirmation). `nil` means "the
    /// action's natural default", not "false".
    var flag: Bool? = nil
    /// Which screen side to arrange a window against.
    var side: MacWindowSide? = nil
    /// A pane of System Settings (e.g. `accessibility`).
    var settingsPane: String = ""

    // MARK: - Google

    /// The Google task's title, or the event's title. Carried in `text` for a
    /// task (one free-text field) and in `title` for an event, so the two
    /// writes cannot be confused by a shared field.
    var title: String = ""
    /// The event's start, as the person stated it ("2026-10-30T10:00:00+05:30"
    /// or "tomorrow at 10 AM"). Validated by the backend, which is the side
    /// that owns the time parser.
    var startDate: String = ""
    /// The event's duration. Zero means the backend's default of an hour.
    var durationMinutes: Int? = nil
    /// A location, for an event.
    var location: String = ""
    /// Which Google task list. Empty means the connected account's default.
    var taskListID: String = ""

    // MARK: - Files and notifications

    /// The new name for a rename (a single path component, never a path).
    var newName: String = ""
    /// The notification's title, and a short body for a notification or a
    /// clipboard write reuses `text`.
    var notificationTitle: String = ""
    /// A clipboard or notification body.
    var body: String = ""
}

extension MacActionArguments {
    /// What a Google write checks its required field against.
    ///
    /// A task has no title field of its own: `text` carries it, because a flow
    /// step is one free-text value and inventing a second field for the same
    /// thing would let a step carry two different titles.
    var googleTitle: String {
        get { title.isEmpty ? text : title }
        set { title = newValue }
    }
}

enum MacWindowSide: String, Codable, CaseIterable {
    case left, right, maximise

    var displayName: String {
        switch self {
        case .left: return "left half"
        case .right: return "right half"
        case .maximise: return "maximised (visible area)"
        }
    }
}

/// How much a person should be asked before this runs.
///
/// Deliberately separate from the backend's `RiskLevel`, because the two answer
/// different questions. The backend's tier decides whether the *conversation*
/// needs an approval card; this decides whether the *native executor* will act
/// now, needs one explicit confirmation, or refuses outright. The native
/// boundary must be able to answer that on its own — a LAN client that reached
/// the backend must not be able to talk a Class-A action into executing here.
enum MacRiskClass: String, Codable {
    /// Read-only. No state changes; runs without extra prompting.
    case read = "A"
    /// Reversible, low-impact. Runs when the request is unambiguous.
    case low = "B"
    /// Changes files/content. Confirmation when it could lose or overwrite data.
    case mutating = "C"
    /// High-impact. Explicit confirmation every time, showing exactly what
    /// will happen. Never inferred.
    case destructive = "D"

    var displayName: String {
        switch self {
        case .read: return "Read-only"
        case .low: return "Low risk"
        case .mutating: return "Changes data"
        case .destructive: return "High impact"
        }
    }

    /// Whether the executor may run this for an *unattended* caller (backend,
    /// phone, model) when the person did not press the button themselves.
    var requiresConfirmation: Bool {
        switch self {
        case .read, .low: return false
        case .mutating, .destructive: return true
        }
    }
}

/// The macOS privacy permission an action depends on, if any.
enum MacPermission: String, Codable, CaseIterable {
    case accessibility
    case automation
    case notifications

    var displayName: String {
        switch self {
        case .accessibility: return "Accessibility"
        case .automation: return "Automation (Apple Events)"
        case .notifications: return "Notifications"
        }
    }

    var settingsURL: URL? {
        switch self {
        case .accessibility:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
        case .automation:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")
        case .notifications:
            return URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension")
        }
    }
}

/// One row of the capability table. Immutable, so the executor cannot be talked
/// into a looser policy at runtime.
struct MacActionPolicy: Equatable {
    let kind: MacActionKind
    let risk: MacRiskClass
    /// Which arguments must be non-empty before the action is even considered.
    let required: [KeyPath<MacActionArguments, String>]
    /// The permission this action needs, if the operation itself requires one.
    let permission: MacPermission?
    /// Why the action needs `permission`, in a sentence a person can act on.
    let permissionReason: String

    static let all: [MacActionKind: MacActionPolicy] = Dictionary(
        uniqueKeysWithValues: [
            MacActionPolicy(kind: .listRunningApps, risk: .read, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .frontmostApp, risk: .read, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .openApplication, risk: .low, required: [\MacActionArguments.application],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .activateApplication, risk: .low, required: [\MacActionArguments.application],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .openURL, risk: .low, required: [\MacActionArguments.url],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .openFile, risk: .low, required: [\MacActionArguments.path],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .openFolder, risk: .low, required: [\MacActionArguments.path],
                            permission: nil, permissionReason: ""),
            // Quitting can lose unsaved work, and it changes another app's
            // state without an undo — Class C, not B.
            MacActionPolicy(kind: .quitApplication, risk: .mutating, required: [\MacActionArguments.application],
                            permission: .automation,
                            permissionReason: "Quitting another application sends it an Apple Event."),
            MacActionPolicy(kind: .listWindows, risk: .read, required: [],
                            permission: .accessibility,
                            permissionReason: "Reading another application's windows uses the Accessibility API."),
            MacActionPolicy(kind: .minimizeWindow, risk: .low, required: [],
                            permission: .accessibility,
                            permissionReason: "Moving or minimising another application's window uses the Accessibility API."),
            // Closing a window may discard a document, so it is Class C.
            MacActionPolicy(kind: .closeWindow, risk: .mutating, required: [],
                            permission: .accessibility,
                            permissionReason: "Closing another application's window uses the Accessibility API."),
            MacActionPolicy(kind: .arrangeWindow, risk: .low, required: [],
                            permission: .accessibility,
                            permissionReason: "Moving or resizing another application's window uses the Accessibility API."),
            MacActionPolicy(kind: .focusWindow, risk: .low, required: [],
                            permission: .accessibility,
                            permissionReason: "Raising another application's window uses the Accessibility API."),
            MacActionPolicy(kind: .systemInfo, risk: .read, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .getVolume, risk: .read, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .setVolume, risk: .low, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .setMuted, risk: .low, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .openSystemSettings, risk: .low, required: [],
                            permission: nil, permissionReason: ""),
            // Reads. They reach the network through the local backend, which
            // holds the Google token; nothing on this Mac changes.
            MacActionPolicy(kind: .googleAgenda, risk: .read, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .googleOverdueTasks, risk: .read, required: [],
                            permission: nil, permissionReason: ""),
            // Remote writes: a task or event in the person's own Google
            // account. Class C — it leaves the machine and creates a record
            // they will see elsewhere, and it needs an explicit yes.
            MacActionPolicy(kind: .googleCreateTask, risk: .mutating,
                            required: [\MacActionArguments.text],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .googleCreateEvent, risk: .mutating,
                            required: [\MacActionArguments.title, \MacActionArguments.startDate],
                            permission: nil, permissionReason: ""),
            // Navigation. It changes which screen is showing, nothing else.
            MacActionPolicy(kind: .openToday, risk: .low, required: [],
                            permission: nil, permissionReason: ""),
            // The clipboard holds whatever the person last copied — often a
            // password or a private note. Reading it is Class B, not A, so it
            // is never a silent read on a backend-originated request.
            MacActionPolicy(kind: .readClipboard, risk: .low, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .writeClipboard, risk: .low,
                            required: [\MacActionArguments.text],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .postNotification, risk: .low,
                            required: [\MacActionArguments.text],
                            permission: nil, permissionReason: ""),
            // File mutations. `move` is Class C (it can overwrite at the
            // destination, which is checked before use); `rename` is C for the
            // same reason; `delete` is D — it cannot be undone.
            MacActionPolicy(kind: .moveFile, risk: .mutating,
                            required: [\MacActionArguments.path, \MacActionArguments.destination],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .renameFile, risk: .mutating,
                            required: [\MacActionArguments.path, \MacActionArguments.newName],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .deleteFile, risk: .destructive,
                            required: [\MacActionArguments.path],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .displays, risk: .read, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .audioDevices, risk: .read, required: [],
                            permission: nil, permissionReason: ""),
            MacActionPolicy(kind: .currentTime, risk: .read, required: [],
                            permission: nil, permissionReason: "")
        ].map { ($0.kind, $0) }
    )

    static func forKind(_ kind: MacActionKind) -> MacActionPolicy? { all[kind] }
}

/// Why an action was refused before it could run. Every case names a fact the
/// UI can show; none of them is "unknown error".
enum MacControlRefusal: Error, Equatable {
    case unknownAction(String)
    case missingArgument(MacActionKind, String)
    case invalidArgument(MacActionKind, String)
    case permissionRequired(MacPermission, String)
    case targetNotFound(String)
    case ambiguousTarget(String)
    case unsupportedTarget(String)
    case confirmationRequired(MacActionKind)
    case confinedByRisk(MacActionKind)
    case cancelled

    var message: String {
        switch self {
        case .unknownAction(let id):
            return "There is no Mac action called \(id). Nothing was done."
        case .missingArgument(let kind, let field):
            // Plain article, because the field names ("application", "web
            // address") have different vowels and a hard-coded "a" reads as a
            // machine talking.
            return "\(kind.displayName) needs its \(field), and none was given. Nothing was done."
        case .invalidArgument(let kind, let detail):
            return "\(kind.displayName) was refused: \(detail)"
        case .permissionRequired(let permission, let reason):
            return "\(permission.displayName) permission is needed. \(reason) "
                + "Grant it in System Settings → Privacy & Security → \(permission.displayName), "
                + "then press Recheck."
        case .targetNotFound(let what):
            return what
        case .ambiguousTarget(let what):
            return what
        case .unsupportedTarget(let what):
            return what
        case .confirmationRequired(let kind):
            return "\(kind.displayName) needs an explicit confirmation, and none was given. Nothing was done."
        case .confinedByRisk(let kind):
            return "\(kind.displayName) is not available to an unattended request. "
                + "Run it from the Control Mac screen, where you can confirm it."
        case .cancelled:
            return "Cancelled. Nothing further was done."
        }
    }
}