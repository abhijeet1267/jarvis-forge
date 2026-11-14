import Foundation
#if canImport(Darwin)
import Darwin
#endif

/// The five answers `status` can give about who owns the backend port.
///
/// Only `ours` and `legacy` are safe to act on, and a legacy backend only
/// through the explicit migration path. `foreign` and `other-vantage-checkout`
/// are reported, never touched.
enum BackendLifecycleState: String, Codable, CaseIterable {
    case free
    case ours
    case legacy
    case foreign
    case otherVantageCheckout = "other-vantage-checkout"
    /// A state string this build does not know. Never treated as actionable.
    case unknown

    init(rawValueSafe raw: String) {
        self = BackendLifecycleState(rawValue: raw) ?? .unknown
    }

    /// A sentence for people, not the wire value.
    var displayName: String {
        switch self {
        case .free: return "free"
        case .ours: return "ours"
        case .legacy: return "legacy (older build)"
        case .foreign: return "foreign"
        case .otherVantageCheckout: return "other checkout"
        case .unknown: return "unknown"
        }
    }
}

/// Runs `scripts/backend_lifecycle.py` — the single tool that can prove who
/// owns the backend port — and parses its `--json` status.
///
/// Why a script and not more Swift: the decision "is the process on :8765
/// provably ours?" is made from OS evidence (`ps`/`lsof`: uid, cwd, command
/// line) plus `/health/identity`. That logic already exists, is tested, and
/// refuses to signal anything it cannot prove. The app's job here is only to
/// ask it, show the answer, and invoke the explicit recovery action the person
/// chose — never to `pkill` by name from the UI.
///
/// This type never guesses: every failure is a typed `LifecycleError` the UI
/// can state verbatim, and only one operation may run at a time (`busy`), so a
/// second click is a no-op rather than a second process.
actor BackendLifecycle {

    /// The script's actions. `stop` is intentionally not exposed to the UI.
    enum Action: String {
        case status
        case start
        case restart
        case migrate
    }

    enum LifecycleError: LocalizedError, Equatable {
        case scriptMissing(String)
        case pythonMissing([String])
        case timedOut(action: String, seconds: Int)
        case launchFailed(String)
        case badOutput(String)
        case busy

        var errorDescription: String? {
            switch self {
            case .scriptMissing(let path):
                return "Backend lifecycle script not found at \(path)."
            case .pythonMissing(let tried):
                return "No Python interpreter found (tried \(tried.joined(separator: ", ")))."
            case .timedOut(let action, let seconds):
                return "backend_lifecycle.py \(action) did not finish within \(seconds)s."
            case .launchFailed(let message):
                return "Could not run backend_lifecycle.py: \(message)"
            case .badOutput(let detail):
                return "backend_lifecycle.py returned output that could not be read: \(detail)"
            case .busy:
                return "A backend lifecycle operation is already running."
            }
        }
    }

    struct Status: Codable, Equatable {
        let port: Int
        let pid: Int?
        let state: String
        let identity: LifecycleIdentity?
        let code: LifecycleCode?
        /// `nil` means unknown (an older build cannot report its code age) —
        /// deliberately not `false`, which would read as "fresh".
        let stale: Bool?
        let legacy: LifecycleEvidence?
        let safe_to_restart: Bool
        let migratable: Bool

        var kind: BackendLifecycleState { BackendLifecycleState(rawValueSafe: state) }
        var instanceID: String? {
            guard let id = identity?.instance_id, !id.isEmpty else { return nil }
            return id
        }
        var legacyReason: String { legacy?.reason ?? "no evidence reported" }
    }

    /// Every identity field is optional: a legacy or foreign listener answers
    /// `identity: {}`, which must decode rather than fail the whole status.
    struct LifecycleIdentity: Codable, Equatable {
        let service: String?
        let instance_id: String?
        let pid: Int?
        let started_at: Double?
        let app_version: String?
        let revision: String?
        let branch: String?
        let source_root: String?
        let python: String?
    }

    struct LifecycleCode: Codable, Equatable {
        let stale: Bool?
        let started_at: Double?
        let newest_mtime: Double?
        let note: String?
    }

    struct LifecycleEvidence: Codable, Equatable {
        let pid: Int?
        let uid: Int?
        let command: String?
        let cwd: String?
        let executable: String?
        let reason: String?
    }

    /// What an action returned. `ok == false` carries the script's refusal
    /// verbatim, which is exactly what the UI must show.
    struct ActionResult: Equatable {
        let ok: Bool
        let message: String
    }

    // MARK: - Configuration

    /// Seconds allowed per action. `migrate`/`restart` stop a server, wait up
    /// to 15s for it to leave, then start one and wait up to 30s for identity.
    static let statusTimeout: TimeInterval = 10
    static let actionTimeout: TimeInterval = 60

    private var busy = false

    var isBusy: Bool { busy }

    // MARK: - API

    func status(port: Int, repo: String) async throws -> Status {
        guard !busy else { throw LifecycleError.busy }
        busy = true
        defer { busy = false }
        let result = try await run(action: .status, port: port, repo: repo,
                                   extraArguments: ["--json"],
                                   timeout: Self.statusTimeout)
        do {
            return try JSONDecoder().decode(Status.self, from: result.stdout)
        } catch {
            throw LifecycleError.badOutput(Self.describeDecodeFailure(error, result: result))
        }
    }

    /// Runs an acting command (`start`, `restart`, `migrate --confirm`) and
    /// returns the script's own verdict message.
    func perform(_ action: Action, port: Int, repo: String) async throws -> ActionResult {
        guard action != .status else { throw LifecycleError.launchFailed("status is not an action") }
        guard !busy else { throw LifecycleError.busy }
        busy = true
        defer { busy = false }
        let extra = action == .migrate ? ["--confirm"] : []
        let result = try await run(action: action, port: port, repo: repo,
                                   extraArguments: extra, timeout: Self.actionTimeout)
        let message = result.stdoutText.isEmpty ? result.stderrText : result.stdoutText
        return ActionResult(ok: result.exitCode == 0,
                            message: message.isEmpty ? "backend_lifecycle.py \(action.rawValue) exited \(result.exitCode)" : message)
    }

    // MARK: - Process plumbing

    private struct Executed {
        let exitCode: Int32
        let stdout: Data
        let stderr: Data
        var stdoutText: String { Self.clean(stdout) }
        var stderrText: String { Self.clean(stderr) }
        static func clean(_ data: Data) -> String {
            String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        }
    }

    private func run(action: Action, port: Int, repo: String,
                     extraArguments: [String], timeout: TimeInterval) async throws -> Executed {
        let root = URL(fileURLWithPath: (repo as NSString).expandingTildeInPath, isDirectory: true)
        let script = root.appendingPathComponent("scripts/backend_lifecycle.py")
        guard FileManager.default.fileExists(atPath: script.path) else {
            throw LifecycleError.scriptMissing(script.path)
        }
        let python = try resolvePython(repo: root)

        let process = Process()
        process.executableURL = python
        process.arguments = [script.path, action.rawValue, "--port", "\(port)"] + extraArguments
        process.currentDirectoryURL = root
        // Inherit the environment (JARVIS_HOME, JARVIS_BACKEND_PORT): the
        // script must reason about the same home and port this app targets.
        process.environment = ProcessInfo.processInfo.environment

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        let timedOut = LockedFlag()

        // Completion must NOT depend on the caller's run loop: this app asks
        // for status from a SwiftUI `.task`, and the offscreen render harness
        // pumps a plain RunLoop. So the child is started here, exited and
        // drained on a background queue, and only the result is handed back.
        //
        // Both pipes are read concurrently by dedicated queues: reading one to
        // EOF first would let the other fill its buffer and block the child.
        return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Executed, Error>) in
            let box = ContinuationBox(continuation)
            let outBuffer = LockedBuffer()
            let errBuffer = LockedBuffer()

            // ONE synchronous block: start the drain threads, then wait for
            // the process on this same background thread. Never split this
            // across `await`s -- Swift is free to resume a continuation on the
            // main actor, and `waitUntilExit()` there would pump the very run
            // loop the caller's UI needs (observed: main thread parked inside
            // waitUntilExit and the whole app froze).
            DispatchQueue.global().async {
                do {
                    try process.run()
                } catch {
                    box.resume(.failure(LifecycleError.launchFailed(error.localizedDescription)))
                    return
                }
                let drain = DispatchGroup()
                drain.enter()
                DispatchQueue.global().async {
                    outBuffer.append((try? outPipe.fileHandleForReading.readToEnd()) ?? Data())
                    drain.leave()
                }
                drain.enter()
                DispatchQueue.global().async {
                    errBuffer.append((try? errPipe.fileHandleForReading.readToEnd()) ?? Data())
                    drain.leave()
                }
                process.waitUntilExit()
                drain.wait()
                if timedOut.value {
                    box.resume(.failure(LifecycleError.timedOut(action: action.rawValue,
                                                                seconds: Int(timeout))))
                } else {
                    box.resume(.success(Executed(exitCode: process.terminationStatus,
                                                 stdout: outBuffer.data(),
                                                 stderr: errBuffer.data())))
                }
            }

            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                // `process.isRunning` is false only after the child has been
                // launched (a launch failure resumes above and leaves it false).
                guard process.isRunning else { return }
                timedOut.set()
                process.terminate() // SIGTERM
                DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
                    if process.isRunning {
                        kill(process.processIdentifier, SIGKILL)
                    }
                }
            }
        }
    }

    /// The same interpreter the project itself uses when present; otherwise a
    /// system Python. Absence of all of them is a typed error, not a crash.
    private func resolvePython(repo: URL) throws -> URL {
        let candidates = [
            repo.appendingPathComponent(".venv/bin/python").path,
            "/usr/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            return URL(fileURLWithPath: path)
        }
        throw LifecycleError.pythonMissing(candidates)
    }

    private static func describeDecodeFailure(_ error: Error, result: Executed) -> String {
        let stderr = result.stderrText
        let stdoutHead = String(result.stdoutText.prefix(200))
        return "\(error.localizedDescription)"
            + (stderr.isEmpty ? "" : " — stderr: \(String(stderr.prefix(300)))")
            + (stdoutHead.isEmpty ? "" : " — stdout: \(stdoutHead)")
    }
}

// MARK: - Small concurrency helpers

/// Resume-once wrapper so the termination handler, launch failure and timeout
/// paths can race safely without resuming a continuation twice.
private final class ContinuationBox<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T, Error>?

    init(_ continuation: CheckedContinuation<T, Error>) {
        self.continuation = continuation
    }

    func resume(_ result: Result<T, Error>) {
        lock.lock()
        let c = continuation
        continuation = nil
        lock.unlock()
        guard let c else { return }
        switch result {
        case .success(let value): c.resume(returning: value)
        case .failure(let error): c.resume(throwing: error)
        }
    }
}

private final class LockedBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var buffer = Data()

    func append(_ data: Data) {
        guard !data.isEmpty else { return }
        lock.lock(); buffer.append(data); lock.unlock()
    }

    func data() -> Data {
        lock.lock(); defer { lock.unlock() }
        return buffer
    }
}

private final class LockedFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false

    var value: Bool {
        lock.lock(); defer { lock.unlock() }
        return flag
    }

    func set() {
        lock.lock(); flag = true; lock.unlock()
    }
}