import Foundation
import AppKit

/// The app's single entry point for Mac control.
///
/// Owns three things the UI and Command Center both need and that must agree:
///
/// * **One executor.** Every action goes through `MacActionService.execute`, so
///   the risk and permission checks cannot be skipped by choosing another path.
/// * **One history.** Actions and their real outcomes, capped, with the target
///   identifier and risk class but no contents.
/// * **One confirmation rule.** Class C and D actions pause here until a person
///   says yes; the request that was *shown* is the request that runs.
@MainActor
final class MacControlCoordinator: ObservableObject {

    static let shared = MacControlCoordinator()

    @Published private(set) var history: [MacHistoryEntry] = []
    @Published private(set) var permissions: MacPermissionStatus
    @Published private(set) var installedApplications: [String] = []
    @Published private(set) var isRunning = false
    @Published private(set) var progress: Progress?
    /// The names from the last successful "list running apps" read, so the
    /// Applications card can show what is actually running without a second call.
    @Published private(set) var lastRunningApplications: [String] = []

    /// A step that is waiting for a person to confirm it. Rendering this is the
    /// confirmation dialog's job — nothing runs until `confirmPending()`.
    struct PendingConfirmation: Identifiable, Equatable {
        let id = UUID()
        let requests: [MacActionRequest]
        let summary: String
        /// Which screen asked, so the result returns to the same place.
        let origin: MacActionRouter.Origin
    }
    @Published var pending: PendingConfirmation?
    /// A confirmation raised from the global palette. Kept apart from `pending`
    /// because the Control Mac screen renders `pending` inline, while a palette
    /// request must be answered in the full window.
    @Published private(set) var pendingForPalette: PendingConfirmation?

    struct Progress: Equatable {
        let total: Int
        var completed: Int
        var current: String
    }

    private let service = MacControlService()
    /// Google steps reach the backend, which owns the OAuth token. The native
    /// service only records the outcome.
    private let google: GoogleActions = BackendGoogleActions()
    private let router: MacActionRouter
    private let parser = MacCommandParser()
    private var cancellation: MacControlService.Cancellation?
    private let maxHistory = 60

    init() {
        // The permission closure reads the OS on each call rather than caching,
        // so a switch flipped in System Settings is honoured on the next action
        // without relaunching Vantage.
        var status = MacPermissionInspector.current()
        self.permissions = status
        self.router = MacActionRouter(permissions: { MacPermissionInspector.current() })
        _ = status
    }

    // MARK: - State the UI shows

    func refreshPermissions() {
        permissions = MacPermissionInspector.current()
    }

    func refreshApplications() {
        let fm = FileManager.default
        var names = Set<String>()
        for root in ["/Applications", "/System/Applications",
                     "/System/Applications/Utilities",
                     NSHomeDirectory() + "/Applications"] {
            guard let entries = try? fm.contentsOfDirectory(atPath: root) else { continue }
            for entry in entries where entry.hasSuffix(".app") {
                names.insert(String(entry.dropLast(4)))
            }
        }
        installedApplications = names.sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
    }

    func clearHistory() { history.removeAll() }

    // MARK: - Running actions

    /// Run a single action from a UI control. Returns the result so the calling
    /// screen can show it inline as well as in the history.
    @discardableResult
    func perform(_ kind: MacActionKind, _ arguments: MacActionArguments = MacActionArguments(),
                 origin: MacActionRouter.Origin = .local) async -> MacActionResult {
        do {
            let request = try router.prepare(kind: kind, arguments: arguments, origin: origin)
            if request.risk.requiresConfirmation && origin == .local && !request.confirmed {
                pending = PendingConfirmation(
                    requests: [request],
                    summary: "\(kind.displayName) — \(target(of: arguments, kind: kind))",
                    origin: origin)
                return MacActionResult(action: kind, status: .refused,
                                       message: "Waiting for your confirmation.")
            }
            return await run([request])
        } catch let refusal as MacControlRefusal {
            let result = MacActionResult.refused(kind, refusal)
            record(result, target: target(of: arguments, kind: kind))
            return result
        } catch {
            let result = MacActionResult.failed(kind, error.localizedDescription)
            record(result, target: target(of: arguments, kind: kind))
            return result
        }
    }

    /// The person pressed the confirmation button. Runs exactly the requests
    /// that were displayed — not a re-parse of the sentence, which could differ.
    @discardableResult
    func confirmPending() async -> MacActionResult? {
        guard let waiting = pending else { return nil }
        pending = nil
        let confirmed = waiting.requests.map {
            MacActionRequest(kind: $0.kind, arguments: $0.arguments, policy: $0.policy,
                             userInitiated: $0.userInitiated, confirmed: true)
        }
        return await run(confirmed)
    }

    func cancelPending() {
        pending = nil
    }

    /// Stop a multi-step plan between steps. A native call already in flight is
    /// allowed to finish — killing it mid-write would be the un-undoable thing.
    func stop() {
        cancellation?.cancel()
    }

    // MARK: - Global palette entry points

    /// Run one action typed into the palette. Identical validation and execution
    /// to `perform`, but a protected step is parked in `pendingForPalette` so the
    /// full window can show the approval rather than the palette pretending to
    /// have done it.
    func performForPalette(_ kind: MacActionKind,
                           _ arguments: MacActionArguments) async -> MacActionResult {
        do {
            let request = try router.prepare(kind: kind, arguments: arguments, origin: .local)
            if request.risk.requiresConfirmation && !request.confirmed {
                pendingForPalette = PendingConfirmation(
                    requests: [request],
                    summary: "\(kind.displayName) — \(target(of: arguments, kind: kind))",
                    origin: .local)
                return MacActionResult.refused(kind, .confirmationRequired(kind))
            }
            return await run([request])
        } catch let refusal as MacControlRefusal {
            let result = MacActionResult.refused(kind, refusal)
            record(result, target: target(of: arguments, kind: kind))
            return result
        } catch {
            return MacActionResult.failed(kind, error.localizedDescription)
        }
    }

    /// Confirm a palette-raised request and run exactly what was shown.
    @discardableResult
    func confirmPalettePending() async -> MacActionResult? {
        guard let waiting = pendingForPalette else { return nil }
        pendingForPalette = nil
        let confirmed = waiting.requests.map {
            MacActionRequest(kind: $0.kind, arguments: $0.arguments, policy: $0.policy,
                             userInitiated: true, confirmed: true)
        }
        return await run(confirmed)
    }

    func cancelPalettePending() { pendingForPalette = nil }

    /// Run a described sequence from the palette through the same executor.
    func runPlan(_ steps: [MacCommandParser.Step]) async -> MacActionResult? {
        guard pendingForPalette == nil else {
            return MacActionResult.refused(steps[0].kind,
                                           .invalidArgument(steps[0].kind,
                                                            "a confirmation is already open."))
        }
        var requests: [MacActionRequest] = []
        for step in steps {
            do {
                requests.append(try router.prepare(kind: step.kind,
                                                   arguments: step.arguments,
                                                   origin: .local))
            } catch let refusal as MacControlRefusal {
                return MacActionResult.refused(step.kind, refusal)
            } catch {
                return nil
            }
        }
        if requests.contains(where: { $0.risk.requiresConfirmation }) {
            pendingForPalette = PendingConfirmation(
                requests: requests,
                summary: steps.map { "\($0.kind.displayName)" }.joined(separator: " → "),
                origin: .local)
            return MacActionResult.refused(requests[0].kind,
                                           .confirmationRequired(requests[0].kind))
        }
        return await run(requests)
    }

    // MARK: - Command Center entry point

    /// Run a sentence typed on the Control Mac screen. Unlike the Command
    /// Center path this may ask for a confirmation, because a person is present.
    func runTyped(_ text: String) async -> MacActionResult? {
        switch parser.parse(text) {
        case .notMacControl:
            return MacActionResult(action: .frontmostApp, status: .refused,
                                   message: "That is not something Vantage can do on this Mac. "
                                     + "Try “Open Safari” or “set the volume to 30 percent”.")
        case .explanation:
            return MacActionResult(action: .frontmostApp, status: .unverified,
                                   message: "That sounds like a question. Ask it in Command "
                                     + "Center and Vantage will explain rather than act.")
        case .clarification(let question):
            return MacActionResult(action: .frontmostApp, status: .unverified, message: question)
        case .action(let kind, let arguments):
            return await perform(kind, arguments, origin: .local)
        case .plan(let steps):
            var requests: [MacActionRequest] = []
            for step in steps {
                do {
                    requests.append(try router.prepare(kind: step.kind,
                                                       arguments: step.arguments,
                                                       origin: .local))
                } catch let refusal as MacControlRefusal {
                    return MacActionResult.refused(step.kind, refusal)
                } catch { return nil }
            }
            if requests.contains(where: { $0.risk.requiresConfirmation }) {
                pending = PendingConfirmation(
                    requests: requests,
                    summary: steps.map { "\($0.kind.displayName)" }.joined(separator: " → "),
                    origin: .local)
                return MacActionResult(action: requests[0].kind, status: .unverified,
                                       message: "Waiting for your confirmation.")
            }
            return await run(requests)
        }
    }

    /// Try to serve a typed sentence as Mac control.
    ///
    /// Returns `nil` when the sentence is not a Mac-control request, so Command
    /// Center can fall through to the ordinary conversation. An `.explanation`
    /// outcome also returns nil *and* is not executed — the conversation answers
    /// the question.
    func handleCommandCenter(_ text: String) async -> MacActionResult? {
        // Calendar and Google Tasks first, and only when the sentence names
        // one of them. The brief requires a *typed proposal* before a write:
        // "schedule a review tomorrow at 10" produces a summary and stops
        // there. Nothing here creates anything — the person confirms on Today,
        // where the day, time and title are spelled out. That is why this
        // returns a message rather than a result: it is a question, not an
        // action that ran.
        if let proposalReply = calendarProposalReply(text) {
            return MacActionResult(action: .googleCreateEvent, status: .unverified,
                                   message: proposalReply)
        }
        switch parser.parse(text) {
        case .notMacControl, .explanation:
            return nil
        case .clarification(let question):
            return MacActionResult(action: .frontmostApp, status: .unverified, message: question)
        case .action(let kind, let arguments):
            do {
                let request = try router.prepare(kind: kind, arguments: arguments, origin: .backend)
                return await run([request])
            } catch let refusal as MacControlRefusal {
                let result = MacActionResult.refused(kind, refusal)
                record(result, target: target(of: arguments, kind: kind))
                return result
            } catch {
                return nil
            }
        case .plan(let steps):
            var requests: [MacActionRequest] = []
            for step in steps {
                let kind = step.kind
                let arguments = step.arguments
                do {
                    requests.append(try router.prepare(kind: kind, arguments: arguments, origin: .backend))
                } catch let refusal as MacControlRefusal {
                    let result = MacActionResult.refused(kind, refusal)
                    record(result, target: target(of: arguments, kind: kind))
                    return result
                } catch {
                    return nil
                }
            }
            return await run(requests)
        }
    }

    // MARK: - Calendar proposals

    /// A calendar write the person asked for, turned into a proposal rather
    /// than performed.
    ///
    /// Returns nil for anything that is not a calendar-write request, so the
    /// rest of the pipeline is untouched. When it does answer, the answer is
    /// always one of: the full summary of what *would* be created, or the one
    /// fact that is missing. It never writes, and it never says an event was
    /// created.
    private func calendarProposalReply(_ text: String) -> String? {
        guard CalendarProposal.isScheduleRequest(text) else { return nil }
        switch CalendarProposal.interpret(text) {
        case .ready(let proposal):
            AppNavigation.shared.go(.today)
            // The proposal is handed to Today through the one router, and the
            // sentence names the exact day and times so the reply is checkable
            // without opening the screen.
            NotificationCenter.default.post(name: .vantageCalendarProposalReady,
                                            object: nil, userInfo: ["text": text])
            return "Here is what I would create: " + proposal.summary()
                + " Nothing has been written yet — confirm it on Today and I will "
                + "create exactly that."
        case .needsDetail(let question):
            return question
        case .notACalendarRequest:
            return nil
        }
    }

    // MARK: - Execution

    /// Run an ordered list, reporting each step. Stops at the first failure so a
    /// later step never runs on top of a broken prerequisite, and says which
    /// steps completed and which did not.
    private func run(_ requests: [MacActionRequest]) async -> MacActionResult {
        guard !requests.isEmpty else {
            return .failed(.frontmostApp, "Nothing to run.")
        }
        guard !isRunning else {
            return .refused(requests[0].kind,
                            .invalidArgument(requests[0].kind,
                                             "another Mac action is already running."))
        }
        isRunning = true
        let token = MacControlService.Cancellation()
        cancellation = token
        progress = Progress(total: requests.count, completed: 0,
                            current: requests[0].kind.displayName)
        defer {
            isRunning = false
            cancellation = nil
            progress = nil
        }

        var results: [MacActionResult] = []
        for (index, request) in requests.enumerated() {
            if token.isCancelled {
                results.append(.cancelled(request.kind))
                break
            }
            progress = Progress(total: requests.count, completed: index,
                                current: request.kind.displayName)
            // Off the main thread: a native call to an unresponsive app must not
            // freeze the window. The service is not actor-isolated, so this hop
            // is where the UI stays responsive.
            let result: MacActionResult
            if let googleKind = NativeActionExecutor.googleKind(for: request.kind) {
                // A network call first, asynchronously; the service then turns
                // the outcome into its own result vocabulary.
                let outcome = await google.perform(googleKind, arguments: request.arguments)
                result = await Task.detached(priority: .userInitiated) { [service] in
                    service.execute(request, google: outcome, cancellation: token)
                }.value
            } else {
                result = await Task.detached(priority: .userInitiated) { [service] in
                    service.execute(request, cancellation: token)
                }.value
            }
            if result.action == .listRunningApps, result.status == .verified,
               let names = result.details["applications"] {
                lastRunningApplications = names.split(separator: ",")
                    .map { $0.trimmingCharacters(in: .whitespaces) }
            }
            record(result, target: target(of: request.arguments, kind: request.kind))
            results.append(result)
            if result.status == .failed || result.status == .refused || result.status == .cancelled {
                break
            }
        }
        progress = Progress(total: requests.count, completed: results.count,
                            current: results.last?.action.displayName ?? "")

        if results.count == 1 { return results[0] }
        return Self.combine(results, total: requests.count)
    }

    /// One sentence for a sequence, never claiming more than happened.
    ///
    /// `nonisolated` because it reads no main-actor state: it is a pure
    /// function of its arguments. Inheriting `@MainActor` from the class made
    /// a synchronous test call it from a nonisolated context, which Swift 6
    /// rejects outright.
    nonisolated static func combine(_ results: [MacActionResult], total: Int) -> MacActionResult {
        let done = results.filter { $0.status == .verified }.count
        let allVerified = done == total && results.count == total
        let lines = results.map { "\($0.succeeded ? "✓" : ($0.status == .unverified ? "?" : "✕")) "
            + "\($0.action.displayName): \($0.message)" }
        var summary = lines.joined(separator: "\n")
        if results.count < total {
            let remaining = total - results.count
            summary += "\nStopped after \(results.count) of \(total) steps — "
                + "\(remaining) step\(remaining == 1 ? "" : "s") did not run."
        }
        let status: MacActionResult.Status = allVerified
            ? .verified
            : (results.contains { $0.status == .failed } ? .failed : .unverified)
        let kind = results.last?.action ?? .frontmostApp
        return MacActionResult(action: kind, status: status,
                               message: summary,
                               details: results.last?.details ?? [:],
                               durationMS: results.reduce(0) { $0 + $1.durationMS })
    }

    // MARK: - History

    private func record(_ result: MacActionResult, target: String) {
        history.insert(MacHistoryEntry(result: result, target: target), at: 0)
        if history.count > maxHistory { history.removeLast(history.count - maxHistory) }
    }

    /// What the history names for this action. A path or app name — never the
    /// text someone typed into a field, and never a file's contents.
    private func target(of arguments: MacActionArguments, kind: MacActionKind) -> String {
        switch kind {
        case .openApplication, .activateApplication, .quitApplication,
             .listWindows, .minimizeWindow, .closeWindow, .focusWindow:
            return arguments.application.isEmpty ? "foreground app" : arguments.application
        case .openURL:
            return URL(string: arguments.url)?.host ?? arguments.url
        case .openFile, .openFolder:
            return (arguments.path as NSString).lastPathComponent
        case .setVolume, .getVolume:
            return arguments.percent.map { "\($0)%" } ?? "volume"
        case .setMuted:
            return (arguments.flag ?? false) ? "muted" : "unmuted"
        case .openSystemSettings:
            return arguments.settingsPane.isEmpty ? "system settings" : arguments.settingsPane
        case .arrangeWindow:
            return (arguments.side ?? .left).displayName
        case .listRunningApps, .frontmostApp, .systemInfo:
            return "this Mac"
        case .googleAgenda, .googleOverdueTasks:
            return "Google"
        case .googleCreateTask:
            // A task title is what the person named; it is the record's
            // identity to them, so it may appear in the history — the same way
            // an application name does.
            let title = arguments.googleTitle
            return title.isEmpty ? "Google task" : "Google task: \(title)"
        case .googleCreateEvent:
            let title = arguments.title
            return title.isEmpty ? "Google event" : "Google event: \(title)"
        case .openToday:
            return "Today"
        case .readClipboard:
            return "clipboard"
        case .writeClipboard:
            return "clipboard"
        case .postNotification:
            return arguments.notificationTitle.isEmpty ? "notification"
                                                       : arguments.notificationTitle
        case .moveFile, .deleteFile:
            return (arguments.path as NSString).lastPathComponent
        case .renameFile:
            return (arguments.path as NSString).lastPathComponent
        case .displays, .audioDevices:
            return "this Mac"
        case .currentTime:
            return "clock"
        }
    }
}