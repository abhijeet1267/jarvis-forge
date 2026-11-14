import Foundation

/// Turns a requested action plus raw arguments into a `MacActionRequest`, or
/// refuses it with a typed reason.
///
/// This is the only producer of `MacActionRequest`, and it runs *before* any
/// native API. It answers four questions in order, and stops at the first "no":
///
/// 1. Is this a defined action? (The enum makes unknown ids unrepresentable, but
///    a string that arrived over the wire still has to be looked up.)
/// 2. Are the required arguments present, and do they *mean* something valid?
///    A URL must be http(s); a volume must be 0–100; a path must be absolute or
///    a named folder.
/// 3. Does the current process have the permission the action needs?
/// 4. Is the caller allowed to run an action of this risk class unattended?
///
/// Nothing here touches the system beyond *reading* permission state, so every
/// rule is unit-testable with no side effects.
struct MacActionRouter {

    /// Where the request came from. This is the authorization boundary: only a
    /// press on the Local surface counts as the person acting.
    enum Origin: String, Equatable {
        /// A control inside the Vantage window.
        case local
        /// The backend `/command` pipeline, which the phone can also reach.
        case backend
        /// Anything else that reached this process (a URL handler, a test).
        case external

        var isLocalUser: Bool { self == .local }
    }

    let permissions: () -> MacPermissionStatus

    init(permissions: @escaping () -> MacPermissionStatus = { MacPermissionInspector.current() }) {
        self.permissions = permissions
    }

    // MARK: - Entry point

    /// Parse and validate. Throws `MacControlRefusal`; the caller turns that
    /// into a `MacActionResult.refused` so the person sees a reason.
    func prepare(kindID: String, arguments: MacActionArguments, origin: Origin,
                 confirmed: Bool = false) throws -> MacActionRequest {
        guard let kind = MacActionKind(rawValue: kindID),
              let policy = MacActionPolicy.forKind(kind) else {
            throw MacControlRefusal.unknownAction(kindID)
        }
        return try prepare(kind: kind, arguments: arguments, origin: origin, confirmed: confirmed)
    }

    func prepare(kind: MacActionKind, arguments: MacActionArguments, origin: Origin,
                 confirmed: Bool = false) throws -> MacActionRequest {
        guard let policy = MacActionPolicy.forKind(kind) else {
            // A kind with no policy row is a programming error, and refusing is
            // the safe reading of it: it must never fall through to "allowed".
            throw MacControlRefusal.unknownAction(kind.id)
        }

        // 1. Required arguments present.
        for keyPath in policy.required {
            if arguments[keyPath: keyPath].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                throw MacControlRefusal.missingArgument(kind, Self.name(of: keyPath))
            }
        }

        // 2. Arguments that are present must be meaningful.
        let cleaned = try validate(kind: kind, arguments: arguments)

        // 3. Permission, when the action needs one and the process lacks it.
        if !permissions().permits(policy.permission), let permission = policy.permission {
            throw MacControlRefusal.permissionRequired(permission, policy.permissionReason)
        }

        // 4. Risk vs. caller. A phone or the model may read and may run the
        //    low-risk, reversible actions it was explicitly asked for; it may
        //    not change files or close someone's document unattended.
        if origin != .local && policy.risk.requiresConfirmation && !confirmed {
            throw MacControlRefusal.confinedByRisk(kind)
        }
        if origin == .external && policy.risk != .read {
            throw MacControlRefusal.confinedByRisk(kind)
        }

        return MacActionRequest(kind: kind, arguments: cleaned, policy: policy,
                                userInitiated: origin.isLocalUser, confirmed: confirmed)
    }

    // MARK: - Argument validation

    /// Content-level checks. Returns the arguments with names trimmed and
    /// defaults filled, so the executor receives exactly what the router
    /// checked — never the raw input it might re-interpret.
    func validate(kind: MacActionKind, arguments: MacActionArguments) throws -> MacActionArguments {
        var args = arguments
        args.application = arguments.application.trimmingCharacters(in: .whitespacesAndNewlines)
        args.windowTitle = arguments.windowTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        args.settingsPane = arguments.settingsPane.trimmingCharacters(in: .whitespacesAndNewlines)

        switch kind {
        case .openURL:
            let raw = arguments.url.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let url = URL(string: raw), let scheme = url.scheme?.lowercased(),
                  ["http", "https"].contains(scheme), let host = url.host, !host.isEmpty else {
                throw MacControlRefusal.invalidArgument(
                    kind, "\(raw.isEmpty ? "(empty)" : raw) is not an http or https web address. "
                        + "Vantage only opens websites, not file or app URLs.")
            }
            args.url = url.absoluteString

        case .setVolume:
            guard let percent = arguments.percent else {
                throw MacControlRefusal.missingArgument(kind, "percentage")
            }
            guard (0...100).contains(percent) else {
                throw MacControlRefusal.invalidArgument(
                    kind, "volume must be between 0 and 100, not \(percent).")
            }
            args.percent = percent

        case .setMuted:
            guard arguments.flag != nil else {
                throw MacControlRefusal.missingArgument(kind, "mute state")
            }

        case .arrangeWindow:
            if args.side == nil { args.side = .left }

        case .openSystemSettings:
            let allowed = ["accessibility", "automation", "privacy", "sound", "displays", ""]
            guard allowed.contains(args.settingsPane.lowercased()) else {
                throw MacControlRefusal.invalidArgument(
                    kind, "\(args.settingsPane) is not a System Settings page Vantage opens.")
            }
            args.settingsPane = args.settingsPane.lowercased()

        case .openFile, .openFolder:
            do {
                let url = try MacPathGuard.resolve(arguments.path)
                if let refusal = MacPathGuard.refusal(for: url) {
                    // Opening a protected location is a *read* of where it is,
                    // and is allowed for openFolder (Finder only reveals the
                    // folder, it does not read contents). Modifying is not.
                    if kind == .openFile { throw refusal }
                }
                args.path = url.path
            } catch let refusal as MacPathGuard.Refusal {
                throw MacControlRefusal.invalidArgument(kind, refusal.message)
            }

        case .listRunningApps, .frontmostApp, .listWindows, .minimizeWindow,
             .closeWindow, .focusWindow, .systemInfo, .getVolume,
             .openApplication, .activateApplication, .quitApplication:
            break

        case .googleAgenda, .googleOverdueTasks, .openToday,
             .readClipboard, .displays, .audioDevices, .currentTime:
            // No arguments to check: these read, or they navigate.
            break

        case .writeClipboard, .postNotification:
            args.text = arguments.text.trimmingCharacters(in: .whitespacesAndNewlines)
            args.notificationTitle = arguments.notificationTitle
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard args.text.count <= 10_000 else {
                throw MacControlRefusal.invalidArgument(
                    kind, "the text must be 10,000 characters or fewer "
                        + "(this one is \(args.text.count)).")
            }

        case .moveFile, .deleteFile:
            // Both resolve through the path guard, and both refuse a directory:
            // a recursive move or delete from one sentence is exactly the
            // action this layer exists to make impossible.
            do {
                let source = try MacPathGuard.resolve(arguments.path)
                if let refusal = MacPathGuard.refusal(for: source) { throw refusal }
                var isDirectory: ObjCBool = false
                guard FileManager.default.fileExists(atPath: source.path,
                                                     isDirectory: &isDirectory) else {
                    throw MacControlRefusal.targetNotFound(
                        "There is nothing at \(source.lastPathComponent). Nothing was changed.")
                }
                guard !isDirectory.boolValue else {
                    throw MacControlRefusal.invalidArgument(
                        kind, "\(source.lastPathComponent) is a folder, and Vantage only "
                            + "\(kind == .deleteFile ? "deletes" : "moves") single files "
                            + "here. Folders are moved or deleted in Finder.")
                }
                args.path = source.path
                if kind == .moveFile {
                    let destination = try MacPathGuard.resolve(arguments.destination)
                    if let refusal = MacPathGuard.refusal(for: destination) { throw refusal }
                    guard !FileManager.default.fileExists(atPath: destination.path) else {
                        throw MacControlRefusal.invalidArgument(
                            kind, "something already exists at "
                                + "\(destination.lastPathComponent); moving onto it would "
                                + "replace it. Choose another name.")
                    }
                    args.destination = destination.path
                }
            } catch let refusal as MacPathGuard.Refusal {
                throw MacControlRefusal.invalidArgument(kind, refusal.message)
            }

        case .renameFile:
            let newName = arguments.newName.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !newName.isEmpty else {
                throw MacControlRefusal.missingArgument(kind, "new name")
            }
            // A name, not a path: no separators and no traversal.
            guard !newName.contains("/") && newName != "." && newName != ".." else {
                throw MacControlRefusal.invalidArgument(
                    kind, "\(newName) is a path, not a name. Give just the file's new name.")
            }
            do {
                let source = try MacPathGuard.resolve(arguments.path)
                if let refusal = MacPathGuard.refusal(for: source) { throw refusal }
                guard FileManager.default.fileExists(atPath: source.path) else {
                    throw MacControlRefusal.targetNotFound(
                        "There is nothing at \(source.lastPathComponent). Nothing was renamed.")
                }
                args.path = source.path
                args.newName = newName
            } catch let refusal as MacPathGuard.Refusal {
                throw MacControlRefusal.invalidArgument(kind, refusal.message)
            }

        case .googleCreateTask:
            // The required-field check above already refused an empty title.
            // Trim it here so what the backend receives is what was checked.
            let title = arguments.googleTitle
                .trimmingCharacters(in: .whitespacesAndNewlines)
            args.googleTitle = title
            guard title.count <= 300 else {
                throw MacControlRefusal.invalidArgument(
                    kind, "a task title must be 300 characters or fewer "
                        + "(this one is \(title.count)).")
            }

        case .googleCreateEvent:
            let title = arguments.googleTitle
                .trimmingCharacters(in: .whitespacesAndNewlines)
            args.googleTitle = title
            let start = arguments.startDate
                .trimmingCharacters(in: .whitespacesAndNewlines)
            args.startDate = start
            guard title.count <= 300 else {
                throw MacControlRefusal.invalidArgument(
                    kind, "an event title must be 300 characters or fewer.")
            }
            guard start.count <= 64 else {
                throw MacControlRefusal.invalidArgument(
                    kind, "\(start.prefix(40))… is not a date and time Vantage can read.")
            }
            // Parsed here too, so an unreadable time is refused *before* the
            // approval card — the person must never spend a confirmation on a
            // step that cannot run. The backend parses again; neither trusts
            // the other's reading.
            guard CalendarProposal.parseTime(start) != nil
                    || TodayMapping.parseInstant(start) != nil else {
                throw MacControlRefusal.invalidArgument(
                    kind, "\(start) is not a date and time Vantage can read.")
            }
            if let minutes = args.durationMinutes, !(5...600).contains(minutes) {
                throw MacControlRefusal.invalidArgument(
                    kind, "a duration must be between 5 and 600 minutes, not \(minutes).")
            }
        }
        return args
    }

    // MARK: - Helpers

    private static func name(of keyPath: KeyPath<MacActionArguments, String>) -> String {
        switch keyPath {
        case \MacActionArguments.application: return "application"
        case \MacActionArguments.windowTitle: return "window title"
        case \MacActionArguments.url: return "web address"
        case \MacActionArguments.path: return "path"
        case \MacActionArguments.destination: return "destination"
        case \MacActionArguments.text: return "text"
        case \MacActionArguments.title: return "title"
        case \MacActionArguments.startDate: return "start time"
        default: return "argument"
        }
    }
}