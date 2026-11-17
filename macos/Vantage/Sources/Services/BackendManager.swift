import Foundation
import AppKit

/// Manages the Vantage backend process lifecycle:
/// detect if running, start it, wait for health, stop on quit.
@MainActor
final class BackendManager: ObservableObject {

    static let shared = BackendManager()

    enum State: Equatable {
        case unknown
        case checking
        case running
        case starting
        case stopped(reason: String)
        case error(String)
    }

    @Published var state: State = .unknown
    @Published var health: APIModels.HealthResponse?

    /// Who the process on the backend port says it is (`/health/identity`).
    ///
    /// The route is unauthenticated precisely so this question can be asked
    /// before a token exists. It exists because "some HTTP server answered on
    /// :8765" was treated as "the backend is running": a process started a day
    /// earlier served old routing while the app showed "Connected". The
    /// identity route turns that into a stated fact.
    @Published var identity: APIModels.BackendIdentity?

    /// The backend's own verdict that what is running is older than the code
    /// on disk. This is a **warning**, not an outage: the app stays fully
    /// usable (health still polls) and the footer says a restart is needed.
    @Published var stale: Bool = false

    /// The actionable sentence for a running-but-wrong backend (stale build).
    /// Held separately from `state` so a stale backend does not block use of
    /// the app the way a genuine `.error` would.
    @Published var warning: String?

    /// Injectable identity seam, mirroring `healthFetcher`. Production asks
    /// the backend's unauthenticated `/health/identity`; tests and the
    /// integration harness inject a deterministic answer or a specific error
    /// (a 404 stands for a build that predates the route).
    var identityFetcher: @Sendable () async throws -> APIModels.BackendIdentityResponse = {
        try await APIClient.shared.backendIdentity()
    }

    /// Why a health fetch failed. The UI must say "unauthorized" (the backend
    /// answered, so it IS reachable — the token was rejected) differently from
    /// "unreachable" (no backend at all). Conflating the two is how a token problem
    /// reads as a backend outage and the app spins retrying a perfectly-alive server.
    enum HealthFetchError: Error, Equatable {
        case unreachable
        case unauthorized
    }

    /// Injectable health-fetch seam. Production hits the backend's `/health`
    /// with the auth token; tests inject a deterministic stub so state
    /// transitions are testable without a live backend or real timers.
    /// Throws `HealthFetchError.unauthorized` on a 401, `HealthFetchError.unreachable`
    /// on any other non-2xx, and lets a network-level `URLError` (connection refused,
    /// timeout) propagate as the unreachable case.
    ///
    /// The URL is built from `backendURL` (not a hard-coded port) so this always
    /// targets the same backend `checkBackend` probes. `backendURL` honors an
    /// internal `JARVIS_BACKEND_PORT` override (default 8765, unchanged in
    /// production) so an isolated integration test can point the *real* fetcher
    /// and state machine at a local server it controls — no user-facing
    /// port-configuration surface is added.
    var healthFetcher: @Sendable () async throws -> APIModels.HealthResponse = {
        let url = URL(string: "\(BackendManager.backendBaseURL)/health")!
        var req = URLRequest(url: url)
        req.timeoutInterval = 4
        if let tok = APIClient.shared.refreshToken(), !tok.isEmpty {
            req.setValue(tok, forHTTPHeaderField: "X-JARVIS-Token")
        }
        let (data, response) = try await URLSession.shared.data(for: req)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw (http.statusCode == 401 ? HealthFetchError.unauthorized
                                          : HealthFetchError.unreachable)
        }
        return try JSONDecoder().decode(APIModels.HealthResponse.self, from: data)
    }

    /// Poll interval in seconds. Default 30 (production); tests use a short
    /// value so the loop is exercised deterministically.
    var pollInterval: TimeInterval = 30

    private var process: Process?
    private var companionProcess: Process?

    /// The backend's base URL. Production is `http://127.0.0.1:8765`. An
    /// internal `JARVIS_BACKEND_PORT` env override lets an isolated test target
    /// a local server without changing the production default or exposing a
    /// user-facing setting. `checkBackend` and `healthFetcher` both use this so
    /// they always probe the same host. `nonisolated` because the `@Sendable`
    /// health-fetch closure (outside the main actor) reads it.
    nonisolated static var backendBaseURL: String {
        let port = ProcessInfo.processInfo.environment["JARVIS_BACKEND_PORT"] ?? "8765"
        return "http://127.0.0.1:\(port)"
    }

    /// Kept for any existing reference; mirrors `backendBaseURL`'s port.
    var port: Int {
        Int(ProcessInfo.processInfo.environment["JARVIS_BACKEND_PORT"] ?? "8765") ?? 8765
    }

    /// The port `backend_lifecycle.py` inspects and acts on. It is the backend
    /// port in production; the internal `JARVIS_LIFECYCLE_PORT` override lets a
    /// verification harness ask about an isolated fixture port while the app
    /// still talks to its own fake backend. Not a user-facing setting.
    var lifecyclePort: Int {
        Int(ProcessInfo.processInfo.environment["JARVIS_LIFECYCLE_PORT"] ?? "") ?? port
    }
    var backendURL: String { Self.backendBaseURL }
    let healthTimeout: TimeInterval = 15
    private let api = APIClient.shared
    internal private(set) var healthPollTask: Task<Void, Never>?

    /// The exact command that loads the current code into a running backend.
    /// Shown in the stale and old-build notices rather than summoned by a
    /// button: the app does not shell out to kill anything.
    static let restartCommand = ".venv/bin/python scripts/backend_lifecycle.py restart"

    // MARK: - Backend lifecycle (scripts/backend_lifecycle.py)

    /// The script's parsed `status`: who owns the port, from OS evidence plus
    /// `/health/identity`. `nil` until the first check answers.
    @Published var lifecycleState: BackendLifecycle.Status?
    @Published var isLifecycleBusy = false
    /// What the in-flight operation is actually doing, for the spinner. Taken
    /// from the operation itself (a status read says "Checking…" even when the
    /// current port state implies an upgrade exists) — not inferred from the
    /// state the button belongs to.
    @Published var lifecycleBusyLabel: String?
    /// The last action's result, verbatim ("Backend restarted — …" or the
    /// script's refusal). Cleared when a new action starts.
    @Published var lastLifecycleOutcome: String?
    /// A lifecycle check that could not run at all (script/python missing).
    /// Kept apart from `lastLifecycleOutcome` because it is not an outcome.
    @Published var lifecycleError: String?

    /// Runs the script off the main thread with one operation in flight. The
    /// actor enforces busy-ness itself; the flag is mirrored for the UI.
    private let lifecycle = BackendLifecycle()

    /// Re-reads `status --json` and publishes it. Never acts.
    func refreshLifecycle() async {
        guard !isLifecycleBusy else { return }
        isLifecycleBusy = true
        lifecycleBusyLabel = "Checking the backend…"
        defer {
            isLifecycleBusy = false
            lifecycleBusyLabel = nil
        }
        do {
            lifecycleState = try await lifecycle.status(port: lifecyclePort, repo: BackendManager.findProjectDir())
            lifecycleError = nil
        } catch {
            lifecycleError = error.localizedDescription
        }
    }

    /// The one recovery entry point: chooses the action the current state
    /// permits, runs it only when the script can prove the target is ours, and
    /// then re-runs status + health + identity.
    ///
    /// Refusals for `foreign` / `other-vantage-checkout` are decided here as a
    /// second line of defence; the script refuses them again before signalling,
    /// and the UI does not even offer a button.
    @discardableResult
    func migrateOrRestart() async -> String {
        guard !isLifecycleBusy else { return "A backend lifecycle operation is already running." }
        isLifecycleBusy = true
        lifecycleBusyLabel = "Checking the backend…"
        lastLifecycleOutcome = nil
        defer {
            isLifecycleBusy = false
            lifecycleBusyLabel = nil
        }

        let repo = BackendManager.findProjectDir()
        var action: BackendLifecycle.Action
        var prior: BackendLifecycle.Status?
        do {
            prior = try await lifecycle.status(port: lifecyclePort, repo: repo)
            lifecycleState = prior
            guard let status = prior else {
                let message = "No status was returned; nothing was done."
                lastLifecycleOutcome = message
                return message
            }
            switch status.kind {
            case .ours where status.stale == true:
                action = .restart
            case .legacy where status.migratable:
                action = .migrate
            case .free:
                action = .start
            case .ours:
                let message = "Backend is running the current build — no restart needed."
                lastLifecycleOutcome = message
                return message
            case .legacy:
                let message = "The older backend on port \(port) could not be proven safe to migrate: "
                    + status.legacyReason + ". Nothing was changed."
                lastLifecycleOutcome = message
                return message
            case .foreign, .otherVantageCheckout, .unknown:
                let message = Self.refusal(for: status, port: lifecyclePort)
                lastLifecycleOutcome = message
                return message
            }
        } catch {
            let message = error.localizedDescription
            lifecycleError = message
            lastLifecycleOutcome = message
            return message
        }

        let verb: String
        switch action {
        case .restart: verb = "Backend restarted"
        case .migrate: verb = "Legacy backend upgraded"
        case .start: verb = "Backend started"
        case .status: verb = "Backend checked"
        }

        do {
            lifecycleBusyLabel = {
                switch action {
                case .restart: return "Restarting the backend…"
                case .migrate: return "Upgrading the legacy backend…"
                case .start: return "Starting the backend…"
                case .status: return "Checking the backend…"
                }
            }()
            let result = try await lifecycle.perform(action, port: lifecyclePort, repo: repo)
            // Always re-read the facts, successful or not: a failed start may
            // still have left a listener, and a successful one must show the
            // new instance id.
            await refreshLifecycle()
            await refresh()

            let message: String
            if result.ok {
                if let after = lifecycleState, after.kind == .ours {
                    message = "\(verb) — new instance \(after.instanceID ?? "unknown"), "
                        + (after.stale == true ? "code is older than disk (restart again after saving)" : "code current")
                } else {
                    message = "\(verb) — \(result.message)"
                }
            } else {
                message = result.message
            }
            lastLifecycleOutcome = message
            return message
        } catch {
            let message = error.localizedDescription
            lifecycleError = message
            lastLifecycleOutcome = message
            await refreshLifecycle()
            return message
        }
    }

    /// The sentence shown when the port is owned by something this app cannot
    /// prove is its own. Names the evidence and the exact manual command.
    static func refusal(for status: BackendLifecycle.Status, port: Int) -> String {
        let reason = status.legacyReason
        let action = status.kind == .otherVantageCheckout
            ? "stop that checkout's backend from its own folder, or inspect pid \(status.pid.map(String.init) ?? "?")"
            : "stop pid \(status.pid.map(String.init) ?? "?") yourself"
        return "Vantage will not stop a process it cannot prove is its own. "
            + "Port \(port): \(status.kind.displayName). Evidence: \(reason). "
            + "To proceed manually, \(action), or check with "
            + "`.venv/bin/python scripts/backend_lifecycle.py status --port \(port)`."
    }

    // MARK: - Lifecycle

    func connect() async {
        state = .checking
        let owned = await checkBackend()
        if owned {
            // Something already owns the port. Never spawn a second backend
            // over it — `uv run` would fail on the bind or orphan the first
            // process, which would keep answering /health for the UI. Ask the
            // process who it is: `refresh()` consults /health and then
            // /health/identity, and concludes running / stale / foreign / old.
            await refresh()
            if state == .checking {
                // The port answered, but /health neither decoded as healthy
                // nor raised one of its own error messages. Classify by
                // identity before falling back, so this cannot sit at
                // "Checking…" forever.
                await checkIdentity()
            }
            if state == .checking {
                state = .error("The service on \(backendURL) answered, but not as a Vantage backend. "
                               + "Nothing was started. If this is unexpected, inspect what owns port \(port).")
            }
            // Poll whenever the state is recoverable (running, or the stale
            // warning which is still running, or an error worth recovering
            // from); a manual disconnect or a foreign owner is left alone.
            if canRecover {
                startHealthPolling()
            }
        } else {
            await startBackend()
        }
    }

    func disconnect() {
        stopHealthPolling()
        stopCompanionGateway()
        stopBackend()
        identity = nil
        stale = false
        warning = nil
        state = .stopped(reason: "User disconnected")
    }

    /// Reads `/health` and moves the state from *that* answer alone.
    ///
    /// Returns `true` only when this call decoded a healthy payload. The
    /// result matters: `health` keeps its last value, so a later caller
    /// cannot use `health?.status == "ok"` to decide whether *this* poll
    /// succeeded — a poll that failed after a healthy one would otherwise
    /// be treated as healthy and an identity probe would run against a
    /// backend that just went away (or answered 401).
    ///
    /// Deciding *which* Vantage answered (current / stale / foreign / old) is
    /// `refresh()`'s job; this method keeps a single-request contract so the
    /// existing state tests drive exactly it.
    @discardableResult
    func refreshHealth() async -> Bool {
        do {
            health = try await healthFetcher()
            if health?.status == "ok" {
                state = .running
                return true
            }
            return false
        } catch let e as HealthFetchError {
            // A 401 means the backend ANSWERED — it is reachable, but the token was
            // rejected. Say so accurately (and point at the token file) instead of
            // mislabelling it as an outage. A non-401 HTTP error is a reachable
            // backend returning an unexpected payload; a network failure is the
            // genuinely-unreachable case. Both keep polling so the UI recovers when
            // the condition clears.
            let message: String
            switch e {
            case .unauthorized:
                message = "Not authorized — the backend is running but rejected the API token. Check ~/.jarvis-forge/api_token."
            case .unreachable:
                message = "Backend unreachable — retrying…"
                // A non-401 HTTP error from /health still means something is
                // listening; keep the identity out of it — /health did not
                // decode as Vantage, so no identity claim is trusted here.
                identity = nil
                stale = false
                warning = nil
            }
            if state == .running || state == .checking {
                state = .error(message)
            }
            // The token was rejected, but identity is unauthenticated: ask it
            // so Settings can still show which build answered the 401.
            if e == .unauthorized {
                await fetchIdentityQuietly()
            }
            return false
        } catch {
            // Network-level failure (connection refused, timeout) or any other error:
            // the backend is not reachable at all, so any identity from an
            // earlier poll describes a process that is no longer there.
            identity = nil
            stale = false
            warning = nil
            if state == .running || state == .checking {
                state = .error("Backend unreachable — retrying…")
            }
            return false
        }
    }

    /// Read identity without changing `state` — used after a 401, where the
    /// backend is reachable but the token is not (yet) good enough for
    /// `/health`. `/health/identity` needs no token, so Settings can still say
    /// which build is at the other end.
    private func fetchIdentityQuietly() async {
        guard let response = try? await identityFetcher() else { return }
        identity = response.identity
        stale = response.code?.stale ?? false
    }

    /// The full refresh the UI asks for: /health, then /health/identity.
    ///
    /// This is what `connect()`, the poll loop and the Settings "Refresh"
    /// button call. `refreshHealth()` alone answers "did /health decode?" and
    /// is kept for the tests that drive exactly that; the state a *person*
    /// sees is settled here, because a port that answers /health can still be
    /// a stale build, an older build without the identity route, or another
    /// checkout entirely.
    func refresh() async {
        let healthy = await refreshHealth()
        if healthy {
            await checkIdentity()
        }
    }

    /// True if the current state is a running or "backend unreachable" error
    /// state — the states in which a health poll should keep trying so a
    /// recovered backend is picked up automatically.
    ///
    /// `.error` deliberately includes the *foreign backend* and *old build*
    /// cases: the port is owned by the wrong thing today, but the poll is the
    /// only way to notice that it went away (or was restarted into a current
    /// build), and it must never spawn anything. A stale build is `.running`
    /// with a `warning`, so it polls like any healthy connection.
    var canRecover: Bool {
        switch state {
        case .running, .error:
            return true
        default:
            return false
        }
    }

    // MARK: - Internal

    private func checkBackend() async -> Bool {
        guard let url = URL(string: "\(backendURL)/health") else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        do {
            let (_, response) = try await URLSession.shared.data(for: req)
            // Any HTTP response from the port means *something* is listening,
            // so a second backend must NOT be spawned here. It does NOT mean
            // the backend is healthy: a 200 from a foreign service, a stale
            // build, or a 401 from an old token are all "the port is owned".
            // What is actually on the port is decided afterwards by the
            // identity probe (see `checkIdentity`); this function only answers
            // "is the port owned, and by enough of an HTTP server to ask?".
            return (response as? HTTPURLResponse) != nil
        } catch {
            return false
        }
    }

    /// Ask the process on the backend port who it is and decide the state
    /// from the answer. Called after `/health` succeeds (or in `connect()`
    /// when the port is owned but `/health` was inconclusive).
    ///
    /// The five outcomes this distinguishes:
    /// 1. `.running` — a Vantage backend from this checkout, current build.
    /// 2. `.running` + `warning`/`stale` — a Vantage backend from this
    ///    checkout whose code on disk is newer than the running process. The
    ///    app stays usable; the footer and Settings say "restart needed".
    /// 3. `.error` — a Vantage backend from a *different* folder. Connecting
    ///    to it would talk to another checkout's data, so it is an error and
    ///    `connect()` starts nothing.
    /// 4. `.error` — a build that predates `/health/identity` (404 or a
    ///    payload that is not Vantage identity). Calling that healthy is the
    ///    exact defect this endpoint exists to fix: it is an old build.
    /// 5. `.error` — a different service answering Vantage's `/health`
    ///    (service mismatch).
    private func checkIdentity() async {
        let response: APIModels.BackendIdentityResponse
        do {
            response = try await identityFetcher()
        } catch {
            // /health answered, the identity route did not: this is an older
            // Vantage build that predates the route. Not healthy — say so,
            // and name the restart command instead of a generic error.
            identity = nil
            stale = false
            warning = nil
            state = .error("The backend on \(backendURL) is running an older Vantage build that "
                           + "cannot report its identity. Restart it (\(Self.restartCommand)) "
                           + "to load the current code.")
            return
        }

        guard let id = response.identity else {
            // The route answered something, but not with an identity object.
            // A stranger that happens to understand the path is still not
            // this app's backend; do not call it healthy.
            identity = nil
            stale = false
            warning = nil
            state = .error("The service on \(backendURL) answered health checks but did not report "
                           + "a Vantage identity. Nothing was started. If this is unexpected, "
                           + "inspect what owns port \(port).")
            return
        }

        identity = id
        stale = response.code?.stale ?? false

        guard id.service == "vantage-backend" else {
            // A different service entirely. Keep the identity visible in
            // Settings (it says what answered) but never call it running.
            warning = nil
            state = .error("The service on \(backendURL) is not a Vantage backend "
                           + "(it reports itself as \"\(id.service)\"). Nothing was started.")
            return
        }

        if let sourceRoot = id.source_root, !sourceRoot.isEmpty {
            let expected = BackendManager.findProjectDir()
            if !Self.sameDirectory(sourceRoot, expected) {
                warning = nil
                state = .error("Connected to a Vantage backend from a different folder: \(sourceRoot). "
                               + "This app expects \(expected). Stop that backend, or start one from "
                               + "this checkout (\(Self.restartCommand)).")
                return
            }
        }

        if stale {
            warning = "The running backend is older than the code on disk. Restart it to load "
                + "the current build (\(Self.restartCommand))."
            state = .running
            return
        }

        warning = nil
        state = .running
    }

    /// Whether two paths name the same directory, tolerating symlinks, a
    /// trailing slash, and `~`-relative forms. Unreadable paths fall back to a
    /// plain string comparison rather than being reported as different.
    static func sameDirectory(_ a: String, _ b: String) -> Bool {
        let fa = URL(fileURLWithPath: (a as NSString).expandingTildeInPath).resolvingSymlinksInPath().standardizedFileURL
        let fb = URL(fileURLWithPath: (b as NSString).expandingTildeInPath).resolvingSymlinksInPath().standardizedFileURL
        return fa.path == fb.path
    }

    /// One line per lifecycle step, kept in the process and written beside the
    /// profile's log.
    ///
    /// Measured need: when `backendInterpreter` fell back to `/usr/bin/python3`
    /// the backend died before binding a port, and all the app showed was an
    /// empty Home screen. Nothing recorded which of the nine stages had run, so
    /// the failure had to be found by reading the source. A start that fails
    /// before the window renders leaves no other trace.
    private(set) var startupTrace: [String] = []

    func trace(_ event: String, _ detail: String = "") {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let line = detail.isEmpty ? "\(stamp) \(event)" : "\(stamp) \(event) — \(detail)"
        startupTrace.append(line)
        if startupTrace.count > 200 { startupTrace.removeFirst(startupTrace.count - 200) }
        // Best effort: a trace that cannot be written must not stop a start.
        let home = ProcessInfo.processInfo.environment["JARVIS_HOME"]
            ?? (NSHomeDirectory() + "/.jarvis-forge")
        let path = home + "/backend-startup.log"
        if let handle = FileHandle(forWritingAtPath: path) {
            handle.seekToEndOfFile()
            handle.write((line + "\n").data(using: .utf8) ?? Data())
            try? handle.close()
        } else {
            try? (line + "\n").write(toFile: path, atomically: true, encoding: .utf8)
        }
    }

    private func startBackend() async {
        // Idempotent: a start already in flight owns the process. Spawning a
        // second `uv run uvicorn` over it would either fail on the port or
        // orphan the first process, which would keep holding :8765 and make
        // the next health check report a dead UI against a live server.
        guard process == nil else {
            return
        }
        state = .starting

        // Find the jarvis-forge project root
        let projectDir = BackendManager.findProjectDir()
        trace("BACKEND_START_REQUESTED", "projectDir=\(projectDir)")

        // Spawn the checkout's own interpreter through the canonical launcher,
        // not `env uv run uvicorn`.
        //
        // Two reasons, both observed: (1) `/usr/bin/env` resolves whatever `uv`
        // is first on PATH — a sandboxed or differently-installed `uv` cannot
        // find the project's `.venv`, so the app reports "did not respond" and
        // leaves nothing running; (2) `uv run uvicorn jarvis.api.app:app` skips
        // `run.py`, so it ignores the port-in-use refusal, the LAN-mode host,
        // and the HTTPS wiring — the app could then start a *second* backend
        // shaped differently from the one `backend_lifecycle.py` manages. The
        // interpreter path is the same one `backend_lifecycle.py start` uses, so
        // a start from the UI and a start from the CLI produce the same process.
        let interpreter = Self.backendInterpreter(in: projectDir)
        trace("PYTHON_RUNTIME_RESOLVED", interpreter)
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: interpreter)
        proc.arguments = [
            projectDir + "/run.py", "serve",
            "--port", "\(port)",
            "--host", Self.bindHost(),
        ]
        proc.currentDirectoryURL = URL(fileURLWithPath: projectDir)
        // Run from the project root, and resolve modules from it even if the
        // checkout's venv is only partially installed.
        var env = ProcessInfo.processInfo.environment
        // A bundled backend carries its own packages beside `run.py`. Without this
        // a copied app imported whatever the machine's interpreter happened to
        // have, so "standalone" depended on the developer's venv still existing.
        // The checkout layout keeps its packages in the venv instead, and
        // `PYTHONPATH=projectDir` is right there.
        let bundledPackages = projectDir + "/site-packages"
        let paths = FileManager.default.fileExists(atPath: bundledPackages)
            ? [projectDir, bundledPackages]
            : [projectDir]
        env["PYTHONPATH"] = (paths + [env["PYTHONPATH"]].compactMap { $0 }).joined(separator: ":")
        if FileManager.default.fileExists(atPath: bundledPackages) {
            env["PYTHONNOUSERSITE"] = "1"   // ignore ~/.local, which would shadow them
        }
        proc.environment = env

        // Pipe stderr for diagnostics
        let pipe = Pipe()
        proc.standardError = pipe
        proc.standardOutput = pipe

        do {
            try proc.run()
            trace("BACKEND_PROCESS_STARTED", "pid=\(proc.processIdentifier) port=\(port)")
        } catch {
            trace("BACKEND_LAUNCH_FAILED", error.localizedDescription)
            state = .error("Failed to start backend: \(error.localizedDescription)")
            return
        }
        process = proc

        // The companion gateway is owned by the backend itself — see
        // startCompanionGateway for why the app does not spawn it.
        startCompanionGateway(projectDir: projectDir)

        // Wait for health with timeout
        let deadline = Date().addingTimeInterval(healthTimeout)
        while Date() < deadline {
            if await checkBackend() {
                // A port owner alone is not healthy; `refresh` reads
                // /health and then /health/identity and settles the state.
                await refresh()
                startHealthPolling()
                return
            }
            try? await Task.sleep(nanoseconds: 500_000_000) // 0.5s
        }

        state = .error("Backend did not respond within \(Int(healthTimeout))s")
    }

    private func stopBackend() {
        guard let proc = process, proc.isRunning else {
            process = nil
            return
        }
        proc.terminate()
        process = nil
        // Escalate off the main thread: `uv run` may swallow SIGTERM before the
        // Python server sees it, leaving an orphan that keeps :8765 bound. A
        // SIGINT (uvicorn's own stop signal) after a grace period catches that
        // without freezing the UI while the app quits.
        Task.detached { [proc] in
            let grace = Date().addingTimeInterval(5)
            while proc.isRunning && Date() < grace {
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            if proc.isRunning {
                proc.interrupt()
            }
        }
    }

    // MARK: - Health polling

    /// Background poll: calls `/health` every `pollInterval` seconds so the
    /// connection badge reflects reality when the backend goes away.
    internal func startHealthPolling() {
        stopHealthPolling()
        healthPollTask = Task { @MainActor [weak self] in
            while let self, !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(self.pollInterval * 1_000_000_000))
                if Task.isCancelled { break }
                // Only keep polling while the app believes it should be connected
                // (running, or already in the "backend unreachable" error state).
                // Any other state (checking/starting/stopped/unknown) means the
                // lifecycle owns the connection and polling should stand down.
                guard self.canRecover else { break }
                await self.refresh()
            }
        }
    }

    internal func stopHealthPolling() {
        healthPollTask?.cancel()
        healthPollTask = nil
    }

    // MARK: - Companion Gateway

    /// The companion gateway is owned by the backend, not the app.
    ///
    /// The backend spawns and tears down the gateway as part of its own
    /// lifecycle (`jarvis/companion/server.py`), so it cannot outlive the
    /// server it proxies to, and it is reachable from `GET /companion/status`.
    /// If the app also spawned one here, both would race for port 8766 and
    /// whichever lost would either error or leave an orphan the backend's
    /// shutdown can't reach. So the app defers to the backend and only reports
    /// the gateway's state — it no longer starts or stops it itself. Kept as a
    /// no-op so the existing call site and `stopCompanionGateway` stay intact.
    private func startCompanionGateway(projectDir: String) {
        // Intentionally does nothing: the backend is the single owner of the
        // gateway. (Previously this spawned a second uvicorn on 8766.)
    }

    private func stopCompanionGateway() {
        companionProcess?.terminate()
        companionProcess = nil
    }

    /// Where this app's checkout is, resolved once by the same rules the app uses.
    ///
    /// Internal rather than private so the integration harness can seed its fake
    /// backend with *this* value instead of hardcoding a path. Hardcoding is how
    /// the harness and the app drifted apart: the harness still named
    /// `AppData/vantage/jarvis-forge` after the resolver learned to find a
    /// checkout anywhere, so every "same checkout" case failed against a tree
    /// that was in fact the same checkout.
    static func resolvedProjectDir() -> String {
        BackendManager.findProjectDir()
    }

    private static func findProjectDir() -> String {
        if let env = ProcessInfo.processInfo.environment["JARVIS_PROJECT_DIR"],
           FileManager.default.fileExists(atPath: env) {
            return env
        }

        // A backend shipped inside the bundle wins over any checkout: it is the
        // copy this app was built against.
        let bundle = Bundle.main.bundleURL
        if let resource = Bundle.main.resourceURL,
           FileManager.default.fileExists(atPath: resource.path + "/run.py") {
            return resource.path
        }

        // A checkout beside the .app (the development layout).
        for candidate in [bundle.deletingLastPathComponent(), bundle] {
            let path = candidate.path
            if FileManager.default.fileExists(atPath: path + "/run.py") {
                return path
            }
        }

        // Common checkout locations. `AppData/vantage/jarvis-forge` was the only
        // spelling this used to try, so an ordinary clone in `AppData/Vantage` --
        // or in `~/code`, or with any other capitalisation -- found nothing and
        // the app sat there connected to no backend. Search the folders rather
        // than enumerating one guess.
        let home = NSHomeDirectory()
        var roots = [home, home + "/AppData", home + "/code", home + "/dev",
                     home + "/Documents", home + "/Projects", home + "/src"]
        if let names = try? FileManager.default.contentsOfDirectory(atPath: home + "/AppData") {
            roots.append(contentsOf: names.map { home + "/AppData/" + $0 })
        }

        for root in roots {
            if FileManager.default.fileExists(atPath: root + "/run.py")
                && FileManager.default.fileExists(atPath: root + "/jarvis") {
                return root
            }
        }

        // Nothing found. Return the historical default so `startBackend` reports
        // "no backend found at <path>" instead of an empty, unexplained failure.
        return home + "/AppData/vantage/jarvis-forge"
    }

    /// The Python that runs this checkout's backend.
    ///
    /// `.venv/bin/python` is what `backend_lifecycle.py start` uses, so choosing
    /// it here means an app-initiated start and a CLI start are the same process
    /// shape. Fall back to the system interpreter only when the venv is absent;
    /// the start then fails visibly on a missing dependency rather than silently
    /// running the wrong environment.
    nonisolated static func backendInterpreter(in projectDir: String) -> String {
        // A packaged backend records the interpreter it was built against. That
        // file is the only reliable answer inside a bundle, which has no .venv.
        //
        // Measured: without this, a bundled app found no `.venv/bin/python` under
        // Resources and fell back to `/usr/bin/python3`, which cannot import
        // fastapi. The backend then died before binding a port and the app showed
        // an empty Home screen -- the failure the campaign kept calling "the app
        // does not start its backend".
        let recorded = projectDir + "/python_path.txt"
        if let text = try? String(contentsOfFile: recorded, encoding: .utf8) {
            let path = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !path.isEmpty, FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }
        let venv = projectDir + "/.venv/bin/python"
        if FileManager.default.isExecutableFile(atPath: venv) {
            return venv
        }
        return "/usr/bin/python3"
    }

    /// The host the backend should bind, matching `backend_lifecycle.py`'s rule:
    /// LAN mode on means `0.0.0.0` (the companion gateway and the phone need it),
    /// off means loopback. Reading the same switch file keeps an app-initiated
    /// start from silently narrowing or widening the binding relative to a CLI
    /// restart.
    nonisolated static func bindHost(home explicitHome: String? = nil) -> String {
        let home = explicitHome
            ?? ProcessInfo.processInfo.environment["JARVIS_HOME"]
            ?? (NSHomeDirectory() + "/.jarvis-forge")
        let switchPath = home + "/lan_mode"
        guard let text = try? String(contentsOfFile: switchPath, encoding: .utf8) else {
            return "127.0.0.1"
        }
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return ["true", "1", "on", "yes"].contains(value) ? "0.0.0.0" : "127.0.0.1"
    }
}