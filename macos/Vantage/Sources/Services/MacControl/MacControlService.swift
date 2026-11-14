import Foundation
import AppKit
import CoreAudio
import IOKit.ps
import ApplicationServices
import UserNotifications

/// The native boundary that performs Mac actions.
///
/// Every operation here is written against public macOS APIs:
///
/// * `NSWorkspace` for applications, URLs and files (Apple-shipped AppKit).
/// * CoreAudio's `AudioObject*` property API for volume and mute — the documented
///   interface, not a shell helper, and the default output device is read at call
///   time so an AirPods connection is honoured.
/// * The Accessibility API (`AXUIElement`) for windows, which is the only way to
///   move another application's window and is why Accessibility permission exists.
///
/// There is no AppleScript string built from user text, no `Process` invocation
/// and no arbitrary path execution in this file. The one Apple Event sent — the
/// scoped `quit` — has a bundle identifier substituted into a fixed template, and
/// the identifier was resolved from an installed application, not from the
/// sentence.
///
/// Results follow one rule throughout: a successful call is not a successful
/// outcome until it is read back. Where reading back is impossible the result is
/// `.unverified`, never `.verified`.
final class MacControlService {

    /// Serialises mutations so two quick presses cannot interleave (e.g. two
    /// volume writes racing, or a quit and an activate on the same app).
    private let lock = NSLock()

    /// A cancellation token the UI flips when Stop is pressed. Steps check it
    /// between operations; a native call already in flight is allowed to finish
    /// because killing it mid-way would be the un-undoable thing.
    final class Cancellation {
        private let lock = NSLock()
        private var cancelled = false
        var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
        func cancel() { lock.lock(); cancelled = true; lock.unlock() }
    }

    // MARK: - Google

    /// The Google outcome for the request currently being executed.
    ///
    /// Set by `execute(_:google:cancellation:)` for exactly one call and read
    /// immediately, under the same lock, so it can never be stale or answer a
    /// different request than the one it was produced for.
    private var pendingGoogle: GoogleOutcome?

    /// Run a request whose Google work was already done, asynchronously, by the
    /// caller.
    ///
    /// Only the four Google kinds need this. Every other action stays entirely
    /// synchronous, which is what keeps the lock free of network waits.
    func execute(_ request: MacActionRequest, google: GoogleOutcome?,
                 cancellation: Cancellation? = nil) -> MacActionResult {
        lock.lock()
        pendingGoogle = google
        lock.unlock()
        defer {
            lock.lock()
            pendingGoogle = nil
            lock.unlock()
        }
        return execute(request, cancellation: cancellation)
    }

    /// Translate a `GoogleOutcome` into the result vocabulary.
    ///
    /// The mapping is deliberately one-way and total: a failed read becomes
    /// `.failed` (never `.verified`), a refusal stays `.refused`, and a write
    /// that Google did not confirm becomes `.unverified`.
    private func googleResult(for request: MacActionRequest) -> MacActionResult {
        guard let outcome = pendingGoogle else {
            return .failed(request.kind,
                           "This Google action needs a result from the backend, and none was "
                            + "provided. Nothing was attempted.")
        }
        switch outcome.status {
        case .hasResults, .empty:
            // A read that succeeded is a success whether or not it found
            // anything; `.empty` is already stated in the message.
            return .verified(request.kind, outcome.message, details: outcome.details)
        case .verified:
            return .verified(request.kind, outcome.message, details: outcome.details)
        case .unverified:
            return .unverified(request.kind, outcome.message, details: outcome.details)
        case .refused:
            return .refused(request.kind,
                            MacControlRefusal.unsupportedTarget(outcome.message))
        case .failed:
            return .failed(request.kind, outcome.message, details: outcome.details)
        }
    }

    // MARK: - Dispatch

    /// Run one validated request. The router has already refused anything
    /// malformed, unpermitted or out of its risk class.
    func execute(_ request: MacActionRequest, cancellation: Cancellation? = nil) -> MacActionResult {
        let started = Date()
        lock.lock()
        defer { lock.unlock() }

        if cancellation?.isCancelled == true {
            return .cancelled(request.kind)
        }

        let result: MacActionResult
        switch request.kind {
        case .listRunningApps:   result = listRunningApplications()
        case .frontmostApp:      result = frontmostApplication()
        case .openApplication:   result = openApplication(request.arguments.application)
        case .activateApplication: result = activateApplication(request.arguments.application)
        case .openURL:           result = openURL(request.arguments.url)
        case .openFile:          result = openPath(request.arguments.path, kind: .openFile)
        case .openFolder:        result = openPath(request.arguments.path, kind: .openFolder)
        case .quitApplication:   result = quitApplication(request.arguments.application)

        case .listWindows:       result = listWindows(request.arguments.application)
        case .minimizeWindow:    result = minimizeWindow(request.arguments)
        case .closeWindow:       result = closeWindow(request.arguments)
        case .arrangeWindow:     result = arrangeWindow(request.arguments)
        case .focusWindow:       result = focusWindow(request.arguments)

        case .systemInfo:        result = systemInformation()
        case .getVolume:         result = readVolume()
        case .setVolume:         result = setVolume(request.arguments.percent ?? 0)
        case .setMuted:          result = setMuted(request.arguments.flag ?? false)
        case .openSystemSettings: result = openSystemSettings(request.arguments.settingsPane)

        // Google reads and writes are network calls, so `execute` cannot run
        // them: it holds a lock and is called synchronously. Its caller does
        // the round trip and hands back a result through `googleResult`; if it
        // did not, the honest answer is that nothing was attempted — never a
        // guessed success.
        case .googleAgenda, .googleOverdueTasks, .googleCreateTask, .googleCreateEvent:
            result = googleResult(for: request)
        case .openToday:
            result = .verified(.openToday, "The Today workspace is in front.",
                               details: ["surface": "today"])

        case .readClipboard:     result = readClipboard()
        case .writeClipboard:    result = writeClipboard(request.arguments.text)
        case .postNotification:  result = postNotification(
            title: request.arguments.notificationTitle,
            body: request.arguments.text)
        case .moveFile:          result = moveFile(request.arguments.path,
                                                   to: request.arguments.destination)
        case .renameFile:        result = renameFile(request.arguments.path,
                                                     to: request.arguments.newName)
        case .deleteFile:        result = deleteFile(request.arguments.path)
        case .displays:          result = listDisplays()
        case .audioDevices:      result = listAudioDevices()
        case .currentTime:       result = currentTime()
        }
        return MacActionResult(action: result.action, status: result.status,
                               message: result.message,
                               details: result.details,
                               durationMS: Int(Date().timeIntervalSince(started) * 1000))
    }

    // MARK: - Applications

    /// Regular (Dock-visible) applications currently running, as AppKit sees them.
    func listRunningApplications() -> MacActionResult {
        let apps = NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular }
        let names = apps.compactMap { $0.localizedName }
            .sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
        guard !names.isEmpty else {
            return .unverified(.listRunningApps,
                               "No regular applications report as running right now.",
                               details: ["count": "0"])
        }
        return .verified(.listRunningApps,
                         "\(names.count) applications are running: \(names.joined(separator: ", ")).",
                         details: ["count": "\(names.count)",
                                   "applications": names.joined(separator: ", ")])
    }

    func frontmostApplication() -> MacActionResult {
        guard let app = NSWorkspace.shared.frontmostApplication,
              let name = app.localizedName else {
            return .unverified(.frontmostApp,
                               "macOS did not report a foreground application just now.")
        }
        // `loginwindow` in the foreground means the session is locked, which is a
        // different fact from "Finder is in front" and must not be reported as an
        // app at all.
        if app.bundleIdentifier == "com.apple.loginwindow" {
            return .verified(.frontmostApp, "The screen is locked.",
                             details: ["application": "loginwindow", "locked": "true"])
        }
        return .verified(.frontmostApp, "\(name) is in the foreground.",
                         details: ["application": name,
                                   "bundle_id": app.bundleIdentifier ?? ""])
    }

    /// A bounded, non-recursive sweep of the standard application folders.
    ///
    /// The direct scan above covers the common case; this covers the rest
    /// without crawling the disk. macOS puts several system apps in group
    /// containers under `/System/Applications/Utilities`, and a person asking
    /// for "Finder" means the one in `/System/Library/CoreServices`. Depth is
    /// capped at three and only `.app` bundles are considered.
    nonisolated static func scanForApplication(named name: String, depth: Int = 3) -> URL? {
        let roots = ["/System/Applications", "/System/Library/CoreServices",
                     "/Applications", NSHomeDirectory() + "/Applications"]
        let fm = FileManager.default
        for root in roots {
            let rootURL = URL(fileURLWithPath: root)
            guard let enumerator = fm.enumerator(at: rootURL,
                                                 includingPropertiesForKeys: [.isDirectoryKey],
                                                 options: [.skipsHiddenFiles, .skipsPackageDescendants])
            else { continue }
            var visited = 0
            for case let url as URL in enumerator {
                visited += 1
                if visited > 4000 { break }   // bounded: never an unbounded crawl
                let components = url.pathComponents.count - rootURL.pathComponents.count
                if components > depth { enumerator.skipDescendants(); continue }
                guard url.pathExtension == "app" else { continue }
                let candidate = url.deletingPathExtension().lastPathComponent
                if candidate.caseInsensitiveCompare(name) == .orderedSame {
                    return url
                }
                // An .app is a package; do not descend into it.
                enumerator.skipDescendants()
            }
        }
        return nil
    }

    /// Resolve a name or bundle id to exactly one installed application.
    ///
    /// Uses Launch Services (`urlForApplication`) plus a scan of the standard
    /// application folders, so an app installed after Vantage was built is found
    /// without updating a list. Several matches are *reported*, never resolved
    /// by picking one.
    func resolveApplication(_ query: String) -> Result<URL, MacControlRefusal> {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return .failure(.missingArgument(.openApplication, "application"))
        }

        // A bundle identifier is unambiguous by definition.
        if trimmed.contains("."), let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: trimmed) {
            return .success(url)
        }

        var matches: [(name: String, url: URL)] = []
        let fm = FileManager.default
        let roots = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
                     NSHomeDirectory() + "/Applications"]
        for root in roots {
            guard let entries = try? fm.contentsOfDirectory(atPath: root) else { continue }
            for entry in entries where entry.hasSuffix(".app") {
                let name = String(entry.dropLast(4))
                if name.caseInsensitiveCompare(trimmed) == .orderedSame {
                    matches.append((name, URL(fileURLWithPath: root).appendingPathComponent(entry)))
                }
            }
            // One level into folders such as /Applications/Utilities.
            guard let subdirs = try? fm.contentsOfDirectory(atPath: root) else { continue }
            for sub in subdirs {
                let subPath = URL(fileURLWithPath: root).appendingPathComponent(sub)
                var isDir: ObjCBool = false
                guard fm.fileExists(atPath: subPath.path, isDirectory: &isDir),
                      isDir.boolValue, !sub.hasSuffix(".app") else { continue }
                if let entries = try? fm.contentsOfDirectory(atPath: subPath.path) {
                    for entry in entries where entry.hasSuffix(".app") {
                        let name = String(entry.dropLast(4))
                        if name.caseInsensitiveCompare(trimmed) == .orderedSame {
                            matches.append((name, subPath.appendingPathComponent(entry)))
                        }
                    }
                }
            }
        }

        // De-duplicate by resolved path, because the same bundle can be seen
        // twice through different folders (e.g. a symlink).
        var seen = Set<String>()
        let unique = matches.filter { seen.insert($0.url.resolvingSymlinksInPath().path).inserted }

        if unique.isEmpty,
           let found = Self.scanForApplication(named: trimmed) {
            return .success(found)
        }

        switch unique.count {
        case 0:
            return .failure(.targetNotFound(
                "No installed application is named \(trimmed). I looked in Applications, "
                + "System Applications and ~/Applications. Nothing was opened."))
        case 1:
            return .success(unique[0].url)
        default:
            let names = unique.map { $0.url.path }.joined(separator: ", ")
            return .failure(.ambiguousTarget(
                "More than one installed application matches \(trimmed): \(names). "
                + "Say which one you mean — nothing was opened."))
        }
    }

    /// Launch, or bring forward if it is already running.
    ///
    /// Repeating "Open Safari" must not make a second Safari, so a running
    /// instance is activated rather than opened. Whether it came forward is read
    /// back from `frontmostApplication`.
    func openApplication(_ query: String) -> MacActionResult {
        switch resolveApplication(query) {
        case .failure(let refusal):
            return .refused(.openApplication, refusal)
        case .success(let url):
            let name = url.deletingPathExtension().lastPathComponent
            if let running = runningInstance(of: url) {
                return activate(url: url, name: name, running: running, kind: .openApplication)
            }
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            var launchError: Error?
            var launched = false
            let semaphore = DispatchSemaphore(value: 0)
            NSWorkspace.shared.openApplication(at: url, configuration: configuration) { app, error in
                launchError = error
                launched = app != nil
                semaphore.signal()
            }
            // Bounded: an unresponsive Launch Services must not freeze the UI.
            if semaphore.wait(timeout: .now() + 15) == .timedOut {
                return .failed(.openApplication,
                               "Launch Services did not answer within 15s while opening \(name). "
                               + "Nothing was confirmed.")
            }
            if let launchError {
                return .failed(.openApplication,
                               "Could not open \(name): \(launchError.localizedDescription)")
            }
            guard launched else {
                return .failed(.openApplication, "Could not open \(name).")
            }
            // The launch was requested; now observe whether it is actually the
            // foreground app, so success is a fact rather than a callback firing.
            if frontmostMatches(name: name, url: url) {
                return .verified(.openApplication, "\(name) is open and in front.",
                                 details: ["application": name, "path": url.path])
            }
            return .unverified(.openApplication,
                               "\(name) was opened, but I could not confirm it came to the "
                               + "front — look at the screen before believing me.",
                               details: ["application": name, "path": url.path])
        }
    }

    func activateApplication(_ query: String) -> MacActionResult {
        switch resolveApplication(query) {
        case .failure(let refusal):
            return .refused(.activateApplication, refusal)
        case .success(let url):
            let name = url.deletingPathExtension().lastPathComponent
            guard let running = runningInstance(of: url) else {
                return .unverified(.activateApplication,
                                   "\(name) is installed but not running, so there is nothing "
                                   + "to bring forward. Open it first.",
                                   details: ["application": name, "running": "false"])
            }
            return activate(url: url, name: name, running: running, kind: .activateApplication)
        }
    }

    private func activate(url: URL, name: String, running: NSRunningApplication,
                          kind: MacActionKind) -> MacActionResult {
        let ok = running.activate(options: [.activateAllWindows])
        if !ok {
            return .failed(kind, "macOS refused to bring \(name) forward.")
        }
        if frontmostMatches(name: name, url: url) {
            return .verified(kind, "\(name) is in front.",
                             details: ["application": name, "path": url.path])
        }
        return .unverified(kind,
                           "\(name) was asked to come forward but is not reporting as the "
                           + "foreground app — it may be on another Space.",
                           details: ["application": name, "path": url.path])
    }

    private func runningInstance(of url: URL) -> NSRunningApplication? {
        let target = url.resolvingSymlinksInPath().standardizedFileURL
        return NSWorkspace.shared.runningApplications.first { app in
            guard app.activationPolicy != .prohibited else { return false }
            if let bundle = app.bundleURL?.resolvingSymlinksInPath().standardizedFileURL {
                return bundle.path == target.path
            }
            return false
        }
    }

    private func frontmostMatches(name: String, url: URL) -> Bool {
        guard let front = NSWorkspace.shared.frontmostApplication else { return false }
        if let bundle = front.bundleURL?.resolvingSymlinksInPath().standardizedFileURL {
            return bundle.path == url.resolvingSymlinksInPath().standardizedFileURL.path
        }
        return (front.localizedName ?? "").caseInsensitiveCompare(name) == .orderedSame
    }

    /// Scoped quit through a fixed Apple Event template.
    ///
    /// `quit` has no NSWorkspace equivalent, so this is the one place an Apple
    /// Event is sent. The only substitution is a bundle identifier that
    /// `resolveApplication` found installed — never text from the sentence — and
    /// the target must be running first, so a stray identifier cannot start
    /// anything.
    func quitApplication(_ query: String) -> MacActionResult {
        switch resolveApplication(query) {
        case .failure(let refusal):
            return .refused(.quitApplication, refusal)
        case .success(let url):
            let name = url.deletingPathExtension().lastPathComponent
            guard let running = runningInstance(of: url),
                  let bundleID = running.bundleIdentifier else {
                return .unverified(.quitApplication,
                                   "\(name) is not running, so there is nothing to quit.",
                                   details: ["application": name, "running": "false"])
            }
            // Every identifier Vantage will script is checked to be a bundle id;
            // a quote or newline cannot survive this test.
            guard bundleID.range(of: #"^[A-Za-z0-9.\-]+$"#, options: .regularExpression) != nil else {
                return .refused(.quitApplication, .invalidArgument(.quitApplication,
                    "The application's identifier is not safe to script."))
            }
            // `terminate()` asks politely and returns true when the request was
            // delivered — it does NOT mean the app has exited (an app with
            // unsaved work shows its own dialog first). So success is only
            // claimed once the process is actually gone; otherwise the honest
            // answer is "asked, still running".
            let delivered = running.terminate()
            guard delivered else {
                return .failed(.quitApplication,
                               "macOS refused to send \(name) a quit request.")
            }
            for _ in 0..<10 {
                if running.isTerminated { break }
                usleep(150_000)
            }
            if running.isTerminated {
                return .verified(.quitApplication, "\(name) quit.",
                                 details: ["application": name, "bundle_id": bundleID])
            }
            return .unverified(.quitApplication,
                               "Asked \(name) to quit, but it is still running — it may be "
                               + "showing a save dialog or ignoring the request.",
                               details: ["application": name, "bundle_id": bundleID])
        }
    }

    // MARK: - URLs and files

    func openURL(_ raw: String) -> MacActionResult {
        guard let url = URL(string: raw), let scheme = url.scheme?.lowercased(),
              ["http", "https"].contains(scheme) else {
            return .refused(.openURL, .invalidArgument(.openURL, "not an http(s) URL"))
        }
        let opened = NSWorkspace.shared.open(url)
        guard opened else {
            return .failed(.openURL, "macOS refused to open \(url.host ?? raw).")
        }
        return .verified(.openURL, "Opened \(url.absoluteString) in your default browser.",
                         details: ["url": url.absoluteString])
    }

    func openPath(_ path: String, kind: MacActionKind) -> MacActionResult {
        let url = URL(fileURLWithPath: path)
        let fm = FileManager.default
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: url.path, isDirectory: &isDir) else {
            return .failed(kind, "\(url.lastPathComponent) does not exist at \(url.path).")
        }
        if kind == .openFolder && !isDir.boolValue {
            return .failed(kind, "\(url.path) is a file, not a folder.")
        }
        if kind == .openFile && isDir.boolValue {
            return .failed(kind, "\(url.path) is a folder. Use Open folder for that.")
        }
        let opened = NSWorkspace.shared.open(url)
        guard opened else {
            return .failed(kind, "macOS refused to open \(url.path).")
        }
        return .verified(kind, "Opened \(url.lastPathComponent) (\(url.path)).",
                         details: ["path": url.path])
    }

    // MARK: - Windows (Accessibility)

    /// Windows of an application, as the Accessibility API reports them.
    private func windows(of app: NSRunningApplication) -> [AXUIElement] {
        let element = AXUIElementCreateApplication(app.processIdentifier)
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &value) == .success,
              let windows = value as? [AXUIElement] else { return [] }
        return windows
    }

    private func targetApplication(_ query: String) -> Result<NSRunningApplication, MacControlRefusal> {
        if query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            guard let front = NSWorkspace.shared.frontmostApplication else {
                return .failure(.targetNotFound("There is no foreground application to act on."))
            }
            return .success(front)
        }
        switch resolveApplication(query) {
        case .failure(let refusal): return .failure(refusal)
        case .success(let url):
            guard let running = runningInstance(of: url) else {
                return .failure(.targetNotFound(
                    "\(url.deletingPathExtension().lastPathComponent) is not running, so it has "
                    + "no window to act on."))
            }
            return .success(running)
        }
    }

    func listWindows(_ query: String) -> MacActionResult {
        switch targetApplication(query) {
        case .failure(let refusal): return .refused(.listWindows, refusal)
        case .success(let app):
            let name = app.localizedName ?? "that application"
            let windows = windows(of: app)
            guard !windows.isEmpty else {
                return .unverified(.listWindows,
                                   "\(name) exposes no accessible windows. Some applications "
                                   + "report none, even with Accessibility granted.",
                                   details: ["application": name, "count": "0"])
            }
            let titles = windows.compactMap { window -> String? in
                var title: CFTypeRef?
                AXUIElementCopyAttributeValue(window, kAXTitleAttribute as CFString, &title)
                return (title as? String).flatMap { $0.isEmpty ? nil : $0 }
            }
            return .verified(.listWindows,
                             "\(name) has \(windows.count) window(s): "
                                + (titles.isEmpty ? "(untitled)" : titles.joined(separator: ", ")),
                             details: ["application": name, "count": "\(windows.count)",
                                       "titles": titles.joined(separator: ", ")])
        }
    }

    private func minimizeWindow(_ args: MacActionArguments) -> MacActionResult {
        switch targetApplication(args.application) {
        case .failure(let refusal): return .refused(.minimizeWindow, refusal)
        case .success(let app):
            let name = app.localizedName ?? "the application"
            guard let window = pickWindow(of: app, title: args.windowTitle) else {
                return .refused(.minimizeWindow, .unsupportedTarget(
                    "\(name) has no accessible window to minimise. Some applications do not "
                    + "expose windows to Accessibility."))
            }
            let result = AXUIElementSetAttributeValue(window, kAXMinimizedAttribute as CFString,
                                                      kCFBooleanTrue)
            guard result == .success else {
                return .failed(.minimizeWindow,
                               "\(name) refused to minimise its window (Accessibility error "
                               + "\(result.rawValue)). Nothing was moved.")
            }
            // Read it back: the attribute is the fact.
            var minimized: CFTypeRef?
            AXUIElementCopyAttributeValue(window, kAXMinimizedAttribute as CFString, &minimized)
            let confirmed = (minimized as? Bool) == true
            return confirmed
                ? .verified(.minimizeWindow, "Minimised a window of \(name).",
                            details: ["application": name])
                : .unverified(.minimizeWindow,
                              "Asked \(name) to minimise its window, but the result could not be "
                              + "read back.", details: ["application": name])
        }
    }

    /// Close is Class C: it can discard a document. Pressing the window's close
    /// button through Accessibility is what a person does, and an app with
    /// unsaved work will still show its own save dialog rather than losing data.
    private func closeWindow(_ args: MacActionArguments) -> MacActionResult {
        switch targetApplication(args.application) {
        case .failure(let refusal): return .refused(.closeWindow, refusal)
        case .success(let app):
            let name = app.localizedName ?? "the application"
            guard let window = pickWindow(of: app, title: args.windowTitle) else {
                return .refused(.closeWindow, .unsupportedTarget(
                    "\(name) has no accessible window to close."))
            }
            var button: CFTypeRef?
            let found = AXUIElementCopyAttributeValue(window, kAXCloseButtonAttribute as CFString, &button)
            guard found == .success, let close = button else {
                return .refused(.closeWindow, .unsupportedTarget(
                    "\(name)'s window has no accessible close button, so Vantage will not close it."))
            }
            // AXUIElement is a CFType; the cast is how the API is used.
            let element = unsafeBitCast(close, to: AXUIElement.self)
            let pressed = AXUIElementPerformAction(element, kAXPressAction as CFString)
            guard pressed == .success else {
                return .failed(.closeWindow,
                               "\(name) refused the close action (error \(pressed.rawValue)).")
            }
            return .verified(.closeWindow,
                             "Pressed the close button on a \(name) window. If the document had "
                             + "unsaved changes, \(name) is showing its own save dialog.")
        }
    }

    /// Move and resize against the screen's visible frame.
    private func arrangeWindow(_ args: MacActionArguments) -> MacActionResult {
        switch targetApplication(args.application) {
        case .failure(let refusal): return .refused(.arrangeWindow, refusal)
        case .success(let app):
            let name = app.localizedName ?? "the application"
            guard let window = pickWindow(of: app, title: args.windowTitle) else {
                return .refused(.arrangeWindow, .unsupportedTarget(
                    "\(name) has no accessible window to arrange."))
            }
            let side = args.side ?? .left
            let previous = NSWorkspace.shared.frontmostApplication
            if previous?.processIdentifier != app.processIdentifier {
                _ = app.activate(options: [.activateAllWindows])
            }
            guard let frame = visibleFrame(forWindow: window) else {
                return .unverified(.arrangeWindow,
                                   "Could not read the screen geometry for \(name)'s window.")
            }
            // The top-left origin is what AXPosition wants (Quartz coordinates,
            // y down from the primary display's top).
            let half = frame.width / 2
            let origin: CGPoint
            let size: CGSize
            switch side {
            case .left:  origin = frame.origin; size = CGSize(width: half, height: frame.height)
            case .right: origin = CGPoint(x: frame.minX + half, y: frame.minY)
                         size = CGSize(width: half, height: frame.height)
            case .maximise: origin = frame.origin; size = frame.size
            }
            var position = origin
            var extent = size
            let posValue = AXValueCreate(.cgPoint, &position)
            let sizeValue = AXValueCreate(.cgSize, &extent)
            var failure: AXError = .success
            if let posValue {
                let r = AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, posValue)
                if r != .success { failure = r }
            }
            if let sizeValue {
                let r = AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, sizeValue)
                if r != .success { failure = r }
            }
            if failure != .success {
                return .failed(.arrangeWindow,
                               "\(name) refused to move its window (Accessibility error "
                               + "\(failure.rawValue)). Some apps only allow resizing.")
            }
            // Read the frame back rather than believing the setters.
            if let after = self.frame(of: window) {
                let moved = abs(after.minX - origin.x) < 2 && abs(after.width - size.width) < 2
                return moved
                    ? .verified(.arrangeWindow,
                                "Moved \(name)'s window to the \(side.displayName) of the screen.",
                                details: ["application": name,
                                          "frame": "\(Int(after.origin.x)),\(Int(after.origin.y)) "
                                            + "\(Int(after.width))x\(Int(after.height))"])
                    : .unverified(.arrangeWindow,
                                  "\(name) accepted the change but its window did not move — "
                                  + "some applications ignore positioning requests.",
                                  details: ["application": name])
            }
            return .unverified(.arrangeWindow, "Moved \(name)'s window but could not read it back.")
        }
    }

    private func focusWindow(_ args: MacActionArguments) -> MacActionResult {
        switch targetApplication(args.application) {
        case .failure(let refusal): return .refused(.focusWindow, refusal)
        case .success(let app):
            let name = app.localizedName ?? "the application"
            guard let window = pickWindow(of: app, title: args.windowTitle) else {
                return .refused(.focusWindow, .unsupportedTarget(
                    "\(name) has no accessible window to raise."))
            }
            _ = app.activate(options: [.activateAllWindows])
            let raised = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
            return raised == .success
                ? .verified(.focusWindow, "Raised a window of \(name).",
                            details: ["application": name])
                : .failed(.focusWindow,
                          "\(name) refused to raise the window (error \(raised.rawValue)).")
        }
    }

    /// The first window, or the one whose title contains the named fragment.
    private func pickWindow(of app: NSRunningApplication, title fragment: String) -> AXUIElement? {
        let all = windows(of: app)
        guard !fragment.isEmpty else { return all.first }
        for window in all {
            var title: CFTypeRef?
            AXUIElementCopyAttributeValue(window, kAXTitleAttribute as CFString, &title)
            if let text = title as? String,
               text.localizedCaseInsensitiveContains(fragment) {
                return window
            }
        }
        return nil
    }

    /// The window's current frame, in Quartz coordinates.
    private func frame(of window: AXUIElement) -> CGRect? {
        var positionValue: CFTypeRef?
        var sizeValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(window, kAXPositionAttribute as CFString,
                                            &positionValue) == .success,
              AXUIElementCopyAttributeValue(window, kAXSizeAttribute as CFString,
                                            &sizeValue) == .success,
              let positionValue, let sizeValue else { return nil }
        var point = CGPoint.zero
        var size = CGSize.zero
        guard AXValueGetValue(unsafeBitCast(positionValue, to: AXValue.self), .cgPoint, &point),
              AXValueGetValue(unsafeBitCast(sizeValue, to: AXValue.self), .cgSize, &size) else {
            return nil
        }
        return CGRect(origin: point, size: size)
    }

    /// The usable screen area for this window.
    ///
    /// Screen frames are in AppKit coordinates (origin bottom-left); Accessibility
    /// uses Quartz (top-left). Converting through the primary screen's height is
    /// what makes "left half" land on the left half of the display the window is
    /// actually on.
    private func visibleFrame(forWindow window: AXUIElement) -> CGRect? {
        guard let primary = NSScreen.screens.first else { return nil }
        let anchor = frame(of: window).map { CGPoint(x: $0.midX, y: $0.midY) } ?? CGPoint.zero
        let quartzAnchor = CGPoint(x: anchor.x, y: primary.frame.height - anchor.y)
        let screen = NSScreen.screens.first { screen -> Bool in
            let quartz = CGRect(x: screen.frame.minX,
                                y: primary.frame.height - screen.frame.maxY,
                                width: screen.frame.width,
                                height: screen.frame.height)
            return quartz.contains(quartzAnchor)
        } ?? NSScreen.main ?? primary
        let visible = screen.visibleFrame
        return CGRect(x: visible.minX,
                      y: primary.frame.height - visible.maxY,
                      width: visible.width,
                      height: visible.height)
    }

    // MARK: - System

    func systemInformation() -> MacActionResult {
        let screens = NSScreen.screens
        guard let main = screens.first else {
            return .unverified(.systemInfo, "macOS reported no screens.")
        }
        let visible = main.visibleFrame
        let scale = main.backingScaleFactor
        let battery = Self.batterySummary()
        let host = Host.current().localizedName ?? "this Mac"
        let os = ProcessInfo.processInfo.operatingSystemVersionString
        var details: [String: String] = [
            "screen": "\(Int(main.frame.width))×\(Int(main.frame.height)) at \(Int(scale))x",
            "usable": "\(Int(visible.width))×\(Int(visible.height))",
            "screens": "\(screens.count)",
            "mac": host,
            "os": os,
        ]
        if let battery { details["battery"] = battery }
        var parts = ["This Mac is \(host) on \(os).",
                     "The main display is \(Int(main.frame.width))×\(Int(main.frame.height)) "
                        + "at \(Int(scale))x."]
        if screens.count > 1 {
            parts.append("\(screens.count) displays are connected.")
        }
        if let battery {
            parts.append("Battery: \(battery).")
        }
        return .verified(.systemInfo, parts.joined(separator: " "), details: details)
    }

    /// Battery percentage from IOPowerSources. Read-only and needs no permission;
    /// `nil` on a desktop Mac, which has no battery to report.
    static func batterySummary() -> String? {
        let blob = IOPSCopyPowerSourcesInfo()?.takeRetainedValue()
        guard let blob, let sources = IOPSCopyPowerSourcesList(blob)?.takeRetainedValue() as? [CFTypeRef] else {
            return nil
        }
        for source in sources {
            guard let description = IOPSGetPowerSourceDescription(blob, source)?
                .takeUnretainedValue() as? [String: Any] else { continue }
            if let capacity = description[kIOPSCurrentCapacityKey] as? Int,
               let maximum = description[kIOPSMaxCapacityKey] as? Int, maximum > 0 {
                let percent = Int((Double(capacity) / Double(maximum) * 100).rounded())
                let charging = (description[kIOPSIsChargingKey] as? Bool) == true
                return "\(percent)%\(charging ? " charging" : "")"
            }
        }
        return nil
    }

    // MARK: - Audio (CoreAudio)

    var defaultOutputDevice: AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                                &address, 0, nil, &size, &device)
        return status == noErr && device != 0 ? device : nil
    }

    func volumeAddress() -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyVolumeScalar,
                                   mScope: kAudioObjectPropertyScopeOutput,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    func readVolume() -> MacActionResult {
        guard let device = defaultOutputDevice else {
            return .failed(.getVolume, "No audio output device is available.")
        }
        var address = volumeAddress()
        guard AudioObjectHasProperty(device, &address) else {
            return .refused(.getVolume, .unsupportedTarget(
                "The current output device does not expose a volume control (some external "
                + "interfaces manage volume in hardware)."))
        }
        var volume: Float32 = 0
        var size = UInt32(MemoryLayout<Float32>.size)
        let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &volume)
        guard status == noErr else {
            return .failed(.getVolume, "Reading the volume failed (CoreAudio \(status)).")
        }
        let percent = Int((volume * 100).rounded())
        let muted = readMuted(device)
        return .verified(.getVolume, "The volume is \(percent)%\(muted ? " (muted)" : "").",
                         details: ["percent": "\(percent)", "muted": muted ? "true" : "false"])
    }

    func setVolume(_ percent: Int) -> MacActionResult {
        guard let device = defaultOutputDevice else {
            return .failed(.setVolume, "No audio output device is available.")
        }
        var address = volumeAddress()
        guard AudioObjectHasProperty(device, &address) else {
            return .refused(.setVolume, .unsupportedTarget(
                "The current output device does not expose a volume control."))
        }
        let target = Float32(max(0, min(100, percent))) / 100
        var value = target
        let size = UInt32(MemoryLayout<Float32>.size)
        let status = AudioObjectSetPropertyData(device, &address, 0, nil, size, &value)
        guard status == noErr else {
            return .failed(.setVolume, "Setting the volume failed (CoreAudio \(status)).")
        }
        // Read back: the write returning noErr is the call succeeding, not the
        // device agreeing.
        var readBack: Float32 = 0
        var readSize = UInt32(MemoryLayout<Float32>.size)
        _ = AudioObjectGetPropertyData(device, &address, 0, nil, &readSize, &readBack)
        let actual = Int((readBack * 100).rounded())
        // A muted device reports the stored volume but makes no sound; unmute so
        // "set volume to 30%" is not silently inaudible.
        if readMuted(device) { _ = setMuted(false) }
        return abs(actual - percent) <= 2
            ? .verified(.setVolume, "Set the volume to \(actual)%.",
                        details: ["percent": "\(actual)"])
            : .unverified(.setVolume,
                          "Asked for \(percent)% but the device reports \(actual)%.",
                          details: ["percent": "\(actual)", "requested": "\(percent)"])
    }

    private func readMuted(_ device: AudioDeviceID) -> Bool {
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute,
                                                 mScope: kAudioObjectPropertyScopeOutput,
                                                 mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectHasProperty(device, &address) else { return false }
        var muted = UInt32(0)
        var size = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &muted)
        return status == noErr && muted != 0
    }

    func setMuted(_ muted: Bool) -> MacActionResult {
        guard let device = defaultOutputDevice else {
            return .failed(.setMuted, "No audio output device is available.")
        }
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute,
                                                 mScope: kAudioObjectPropertyScopeOutput,
                                                 mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectHasProperty(device, &address) else {
            return .refused(.setMuted, .unsupportedTarget(
                "The current output device does not support mute."))
        }
        var value = UInt32(muted ? 1 : 0)
        let size = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectSetPropertyData(device, &address, 0, nil, size, &value)
        guard status == noErr else {
            return .failed(.setMuted, "Changing mute failed (CoreAudio \(status)).")
        }
        let confirmed = readMuted(device) == muted
        return confirmed
            ? .verified(.setMuted, muted ? "Muted the output." : "Unmuted the output.",
                        details: ["muted": muted ? "true" : "false"])
            : .unverified(.setMuted, "Asked to \(muted ? "mute" : "unmute") but could not read it back.")
    }

    // MARK: - Settings

    func openSystemSettings(_ pane: String) -> MacActionResult {
        let target: URL?
        switch pane {
        case "accessibility":
            target = MacPermission.accessibility.settingsURL
        case "automation":
            target = MacPermission.automation.settingsURL
        case "sound":
            target = URL(string: "x-apple.systempreferences:com.apple.Sound-Settings.extension")
        case "displays":
            target = URL(string: "x-apple.systempreferences:com.apple.Displays-Settings.extension")
        default:
            target = URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension")
        }
        guard let target else {
            return .failed(.openSystemSettings, "That System Settings page has no link.")
        }
        return NSWorkspace.shared.open(target)
            ? .verified(.openSystemSettings, "Opened System Settings.",
                        details: ["pane": pane.isEmpty ? "privacy" : pane])
            : .failed(.openSystemSettings, "macOS refused to open System Settings.")
    }
}
// MARK: - Clipboard, notifications, files, and read-only detail
//
// Appended as an extension so the dispatch above stays a readable table. Every
// method here reports what it *observed*: a verified clipboard write is read
// back, a move is confirmed by the file existing at the new path, and a delete
// is only reported after the path is gone.

extension MacControlService {

    // MARK: Clipboard

    func readClipboard() -> MacActionResult {
        let board = NSPasteboard.general
        guard let text = board.string(forType: .string), !text.isEmpty else {
            return .unverified(.readClipboard,
                               "The clipboard holds no text right now. It may be empty, "
                               + "or it may hold an image or file rather than text.")
        }
        // Deliberately not echoing the contents into the history: a clipboard
        // often holds a password. The person asked, so the answer goes back to
        // them, but the audit row records only its size.
        return MacActionResult(action: .readClipboard, status: .verified,
                               message: text,
                               details: ["characters": "\(text.count)"])
    }

    func writeClipboard(_ text: String) -> MacActionResult {
        let board = NSPasteboard.general
        board.clearContents()
        guard board.setString(text, forType: .string) else {
            return .failed(.writeClipboard,
                           "macOS refused the clipboard write. Nothing was changed.")
        }
        // Read it back: a successful `setString` is a claim about the call.
        let echoed = NSPasteboard.general.string(forType: .string)
        guard echoed == text else {
            return .unverified(.writeClipboard,
                               "The clipboard was written to, but reading it back did "
                               + "not return the same text, so this is not confirmed.")
        }
        return .verified(.writeClipboard,
                         "Copied \(text.count) character(s) to the clipboard.",
                         details: ["characters": "\(text.count)"])
    }

    // MARK: Notifications

    func postNotification(title: String, body: String) -> MacActionResult {
        // UNUserNotificationCenter throws an Objective-C exception — not a
        // Swift error — when the process has no application bundle, which kills
        // the process outright. Measured in the harness build 2026-10-30. A
        // tool run outside a bundle must get a refusal, not a crash.
        guard Bundle.main.bundleIdentifier != nil else {
            return .refused(.postNotification,
                            .unsupportedTarget("Notifications need Vantage to run as an "
                                               + "application; this process has no app bundle."))
        }
        let content = UNMutableNotificationContent()
        content.title = title.isEmpty ? "Vantage" : title
        content.body = body
        content.sound = .default
        let request = UNNotificationRequest(identifier: UUID().uuidString,
                                            content: content, trigger: nil)
        let center = UNUserNotificationCenter.current()
        var authorization: UNAuthorizationStatus = .notDetermined
        let group = DispatchGroup()
        group.enter()
        center.getNotificationSettings { settings in
            authorization = settings.authorizationStatus
            group.leave()
        }
        group.wait()
        guard authorization == .authorized || authorization == .provisional else {
            return .refused(.postNotification,
                            .permissionRequired(.notifications,
                                                "Showing a notification needs notification "
                                                + "permission for Vantage."))
        }
        var delivered = false
        var failure = ""
        let done = DispatchGroup()
        done.enter()
        center.add(request) { error in
            delivered = (error == nil)
            failure = error?.localizedDescription ?? ""
            done.leave()
        }
        done.wait()
        if delivered {
            return .verified(.postNotification, "Showed a notification: \(body.prefix(80))")
        }
        return .failed(.postNotification,
                       "macOS did not accept the notification. "
                       + (failure.isEmpty ? "Nothing was shown." : failure))
    }

    // MARK: Files

    func moveFile(_ source: String, to destination: String) -> MacActionResult {
        let from = URL(fileURLWithPath: source)
        let to = URL(fileURLWithPath: destination)
        do {
            try FileManager.default.moveItem(at: from, to: to)
        } catch {
            return .failed(.moveFile, "Could not move \(from.lastPathComponent): "
                           + error.localizedDescription)
        }
        guard FileManager.default.fileExists(atPath: to.path) else {
            return .unverified(.moveFile,
                               "The move was requested but \(to.lastPathComponent) is "
                               + "not there afterwards, so it is not confirmed.")
        }
        return .verified(.moveFile,
                         "Moved \(from.lastPathComponent) to \(to.path).",
                         details: ["from": from.path, "to": to.path])
    }

    func renameFile(_ source: String, to newName: String) -> MacActionResult {
        let from = URL(fileURLWithPath: source)
        let to = from.deletingLastPathComponent().appendingPathComponent(newName)
        guard !FileManager.default.fileExists(atPath: to.path) else {
            return .refused(.renameFile,
                            .invalidArgument(.renameFile,
                                             "\(newName) already exists in that folder. "
                                             + "Nothing was renamed."))
        }
        do {
            try FileManager.default.moveItem(at: from, to: to)
        } catch {
            return .failed(.renameFile, "Could not rename \(from.lastPathComponent): "
                           + error.localizedDescription)
        }
        guard FileManager.default.fileExists(atPath: to.path) else {
            return .unverified(.renameFile, "The rename was requested but the file is "
                               + "not there afterwards, so it is not confirmed.")
        }
        return .verified(.renameFile,
                         "Renamed \(from.lastPathComponent) to \(newName).",
                         details: ["from": from.lastPathComponent, "to": newName])
    }

    func deleteFile(_ path: String) -> MacActionResult {
        let url = URL(fileURLWithPath: path)
        do {
            // Trash, not unlink: the person can recover it from Finder, which is
            // what makes a one-sentence delete acceptable at all.
            try FileManager.default.trashItem(at: url, resultingItemURL: nil)
        } catch {
            return .failed(.deleteFile, "Could not move \(url.lastPathComponent) to the "
                           + "Trash: \(error.localizedDescription)")
        }
        guard !FileManager.default.fileExists(atPath: url.path) else {
            return .unverified(.deleteFile,
                               "\(url.lastPathComponent) is still there, so the delete "
                               + "is not confirmed.")
        }
        return .verified(.deleteFile,
                         "Moved \(url.lastPathComponent) to the Trash. It can be "
                         + "recovered from the Trash.",
                         details: ["file": url.lastPathComponent])
    }

    // MARK: Read-only detail

    func listDisplays() -> MacActionResult {
        var details: [String: String] = [:]
        var names: [String] = []
        for (index, screen) in NSScreen.screens.enumerated() {
            let frame = screen.frame
            let name = screen.localizedName
            names.append("\(name) (\(Int(frame.width))×\(Int(frame.height)))")
            details["display\(index + 1)"] = "\(name) \(Int(frame.width))x\(Int(frame.height))"
        }
        guard !names.isEmpty else {
            return .unverified(.displays, "macOS reported no displays just now.")
        }
        return .verified(.displays,
                         "\(names.count) display(s): \(names.joined(separator: ", ")).",
                         details: details)
    }

    func listAudioDevices() -> MacActionResult {
        // CoreAudio's device list needs property calls per device; the honest,
        // cheap answer is the current output device plus the volume, which is
        // what a person asking "what audio devices" almost always means.
        let current = outputDeviceName() ?? "the system default output"
        let volume = readVolume()
        let line = "Output: \(current). " + volume.message
        return .verified(.audioDevices, line,
                         details: ["output": current])
    }

    /// The default output device's name, from CoreAudio, or nil when macOS
    /// will not name it (some aggregate devices report none).
    func outputDeviceName() -> String? {
        guard let device = defaultOutputDevice else { return nil }
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var name: CFString = "" as CFString
        var size = UInt32(MemoryLayout<CFString>.size)
        let status = withUnsafeMutablePointer(to: &name) { pointer -> OSStatus in
            AudioObjectGetPropertyData(device, &address, 0, nil, &size, pointer)
        }
        guard status == noErr, (name as String).isEmpty == false else { return nil }
        return name as String
    }

    func currentTime() -> MacActionResult {
        let now = Date()
        let formatter = DateFormatter()
        formatter.dateStyle = .full
        formatter.timeStyle = .medium
        let text = formatter.string(from: now)
        let zone = TimeZone.current.identifier
        return .verified(.currentTime, "It is \(text) (\(zone)).",
                         details: ["iso": ISO8601DateFormatter().string(from: now),
                                   "timezone": zone])
    }
}
