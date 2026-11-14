import XCTest
@testable import Vantage

@MainActor
final class BackendManagerStateTests: XCTestCase {

    // MARK: - Helpers

    /// A manager wired to a deterministic health fetcher so state transitions
    /// are testable without a live backend or real polling timers.
    private func makeManager(
        result: Result<APIModels.HealthResponse, Error>
    ) -> BackendManager {
        let m = BackendManager()
        m.healthFetcher = {
            switch result {
            case .success(let value): return value
            case .failure(let error): throw error
            }
        }
        return m
    }

    private func okHealth() -> APIModels.HealthResponse {
        // Built by decoding the real wire shape rather than by the synthesized
        // memberwise initializer: that initializer requires every optional
        // field positionally, so `HealthResponse(status:tools:)` does not
        // exist. Decoding also keeps the fixture honest -- a field the server
        // stops sending shows up here.
        let json = Data(#"{"status":"ok","tools":148}"#.utf8)
        do {
            return try JSONDecoder().decode(APIModels.HealthResponse.self, from: json)
        } catch {
            fatalError("the HealthResponse fixture no longer decodes: \(error)")
        }
    }

    // MARK: - State model

    func test_canRecover_reflects_the_relevant_states() {
        let m = BackendManager()
        m.state = .running
        XCTAssertTrue(m.canRecover, "running state should keep polling")
        m.state = .error("Backend unreachable — retrying…")
        XCTAssertTrue(m.canRecover, "unreachable error should keep polling so recovery is possible")
        m.state = .stopped(reason: "User disconnected")
        XCTAssertFalse(m.canRecover, "an explicit disconnect must not be re-polled")
        m.state = .checking
        XCTAssertFalse(m.canRecover, "the lifecycle owns the connection while checking")
        m.state = .unknown
        XCTAssertFalse(m.canRecover, "unknown state must not poll")
    }

    // MARK: - Health fetch → state transition

    func test_successful_health_moves_to_running() async {
        let m = makeManager(result: .success(okHealth()))
        m.state = .checking
        await m.refreshHealth()
        if case .running = m.state {
            // expected
        } else {
            XCTFail("expected .running after a healthy response, got \(m.state)")
        }
        XCTAssertEqual(m.health?.status, "ok")
        XCTAssertEqual(m.health?.tools, 148)
    }

    func test_health_failure_from_running_transitions_to_error() async {
        let m = makeManager(result: .failure(URLError(.notConnectedToInternet)))
        m.state = .running
        await m.refreshHealth()
        if case .error = m.state {
            // expected
        } else {
            XCTFail("expected .error after a failed health check from .running, got \(m.state)")
        }
    }

    func test_auth_failure_is_not_reported_as_unreachable() async {
        // A 401 means the backend ANSWERED (it is reachable) but rejected the token.
        // The UI must say "not authorized" — not "unreachable" — otherwise a token
        // problem reads as a backend outage and the user chases the wrong cause.
        let m = makeManager(result: .failure(BackendManager.HealthFetchError.unauthorized))
        m.state = .running
        await m.refreshHealth()
        guard case .error(let message) = m.state else {
            return XCTFail("expected .error after a 401, got \(m.state)")
        }
        XCTAssertFalse(message.contains("unreachable"),
            "a 401 must not be misreported as an outage, got: \(message)")
        XCTAssertTrue(message.lowercased().contains("authoriz"),
            "a 401 should surface as an authorization problem, got: \(message)")
    }

    func test_http_unreachable_still_reports_unreachable() async {
        // A non-401 HTTP error (or a network failure) is the genuinely-unreachable case.
        let m = makeManager(result: .failure(BackendManager.HealthFetchError.unreachable))
        m.state = .running
        await m.refreshHealth()
        guard case .error(let message) = m.state else {
            return XCTFail("expected .error after an unreachable health check, got \(m.state)")
        }
        XCTAssertTrue(message.lowercased().contains("unreachable"),
            "a non-401 health failure should read as unreachable, got: \(message)")
    }

    func test_health_failure_from_stopped_does_not_overwrite_the_disconnected_state() async {
        // The bug R3.3 guards against: a stale poll must not resurrect a
        // user-initiated disconnect into a scary "unreachable" error.
        let m = makeManager(result: .failure(URLError(.notConnectedToInternet)))
        m.state = .stopped(reason: "User disconnected")
        await m.refreshHealth()
        if case .stopped = m.state {
            // expected: the explicit stop is preserved
        } else {
            XCTFail("a manual disconnect must not become an error state, got \(m.state)")
        }
    }

    func test_recovery_returns_a_failed_state_to_running() async {
        // The full recover sequence: running → (backend dies) → error →
        // (backend back) → running. Each step is driven by the fetcher result.
        var m = makeManager(result: .success(okHealth()))
        m.state = .running
        await m.refreshHealth()
        guard case .running = m.state else {
            return XCTFail("precondition: expected running")
        }

        // Backend dies: swap in a failing fetcher, keep the same manager.
        m.healthFetcher = { throw URLError(.notConnectedToInternet) }
        await m.refreshHealth()
        guard case .error = m.state else {
            return XCTFail("expected error after backend death, got \(m.state)")
        }

        // Backend recovers: swap back to a healthy fetcher.
        m.healthFetcher = { self.okHealth() }
        await m.refreshHealth()
        guard case .running = m.state else {
            return XCTFail("expected recovery to running, got \(m.state)")
        }
    }

    // MARK: - Polling lifecycle

    func test_polling_is_idempotent_and_single_loop() {
        let m = BackendManager()
        m.state = .running
        m.pollInterval = 3600 // long: the test only cares that a loop exists
        m.startHealthPolling()
        XCTAssertNotNil(m.healthPollTask, "polling should create a task")
        let first = m.healthPollTask

        // A second start must cancel the first and create exactly one loop.
        m.startHealthPolling()
        // XCTest has no `XCTAssertNotSame`; comparing the task identities is
        // the same claim and is what the test meant. `healthPollTask` is
        // optional, so require it first, then compare through a closure that
        // unwraps both sides.
        if let firstTask = first, let secondTask = m.healthPollTask {
            XCTAssertFalse(secondTask === firstTask,
                           "a repeated start must not leave two loops")
        } else {
            XCTFail("both starts must create a polling task")
        }
        XCTAssertNotNil(m.healthPollTask)

        m.stopHealthPolling()
        XCTAssertNil(m.healthPollTask, "stop must clear the polling task")
    }

    func test_disconnect_stops_polling() {
        let m = BackendManager()
        m.state = .running
        m.pollInterval = 3600
        m.startHealthPolling()
        XCTAssertNotNil(m.healthPollTask)
        m.stopHealthPolling()
        XCTAssertNil(m.healthPollTask, "disconnect must stop the poll loop")
    }

    func test_polling_stands_down_when_state_is_not_recoverable() async {
        // If the app is in a state where the lifecycle owns the connection
        // (e.g. stopped), a poll must not fire a health check.
        var calls = 0
        let m = BackendManager()
        m.pollInterval = 0.01
        m.healthFetcher = {
            calls += 1
            return self.okHealth()
        }
        m.state = .stopped(reason: "User disconnected")
        m.startHealthPolling()
        // Give the (very short) interval a couple of chances to fire; the
        // loop must break immediately because canRecover is false.
        try? await Task.sleep(nanoseconds: 100_000_000)
        m.stopHealthPolling()
        XCTAssertEqual(calls, 0, "polling must not run while the state is not recoverable")
    }

    // MARK: - Health payload decoding

    func test_health_response_decodes_the_backend_shape() throws {
        // This is the exact shape /health returns (see jarvis/api/app.py).
        // Decoding must not require optional fields to be present.
        let json = """
        {
          "status": "ok",
          "tools": 148,
          "agents": ["laptop", "memory"],
          "llm_enabled": false,
          "ai_provider": "openai",
          "companion": {"running": false, "port": 8766, "detail": "LAN mode is off"}
        }
        """.data(using: .utf8)!
        let h = try JSONDecoder().decode(APIModels.HealthResponse.self, from: json)
        XCTAssertEqual(h.status, "ok")
        XCTAssertEqual(h.tools, 148)
        XCTAssertEqual(h.agents, ["laptop", "memory"])
        XCTAssertEqual(h.ai_provider, "openai")
    }

    func test_health_response_missing_optional_fields_does_not_crash() throws {
        // A minimal payload must decode: only `status` is required.
        let h = try JSONDecoder().decode(
            APIModels.HealthResponse.self,
            from: Data(#"{"status":"ok"}"#.utf8)
        )
        XCTAssertEqual(h.status, "ok")
        XCTAssertNil(h.tools)
    }
}

    // MARK: - Backend spawn: the interpreter and the binding

    func test_the_venv_interpreter_is_preferred_when_it_exists() throws {
        // The same interpreter `backend_lifecycle.py start` uses, so a start
        // from the UI produces the same process shape as one from the CLI.
        let root = NSTemporaryDirectory() + "/vantage-interp-\(Int.random(in: 0..<999999))"
        let bin = root + "/.venv/bin"
        try FileManager.default.createDirectory(atPath: bin, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: root) }
        let venvPython = bin + "/python"
        FileManager.default.createFile(atPath: venvPython, contents: Data("#!/bin/sh\n".utf8))
        try FileManager.default.setAttributes([.posixPermissions: 0o755],
                                              ofItemAtPath: venvPython)
        XCTAssertEqual(BackendManager.backendInterpreter(in: root), venvPython)
    }

    func test_a_checkout_without_a_venv_falls_back_to_the_system_python() {
        let root = NSTemporaryDirectory() + "/vantage-noenv-\(Int.random(in: 0..<999999))"
        try? FileManager.default.createDirectory(atPath: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: root) }
        XCTAssertEqual(BackendManager.backendInterpreter(in: root), "/usr/bin/python3")
    }

    func test_bind_host_follows_the_lan_switch_like_the_lifecycle_tool() throws {
        // LAN mode on is what the phone companion needs; it must not be
        // silently narrowed by an app-initiated start. Off must not widen.
        let home = NSTemporaryDirectory() + "/vantage-home-\(Int.random(in: 0..<999999))"
        try FileManager.default.createDirectory(atPath: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: home) }

        try "true\n".write(toFile: home + "/lan_mode", atomically: true, encoding: .utf8)
        XCTAssertEqual(BackendManager.bindHost(home: home), "0.0.0.0")
        try "0\n".write(toFile: home + "/lan_mode", atomically: true, encoding: .utf8)
        XCTAssertEqual(BackendManager.bindHost(home: home), "127.0.0.1")
        try FileManager.default.removeItem(atPath: home + "/lan_mode")
        XCTAssertEqual(BackendManager.bindHost(home: home), "127.0.0.1")
    }
