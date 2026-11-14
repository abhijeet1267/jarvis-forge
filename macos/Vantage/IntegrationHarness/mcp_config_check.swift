import Foundation

// Proves the Swift client path for `PUT /mcp/config`, end to end, against the
// real backend: the same APIClient and APIModels the app uses. It writes test
// configuration, checks the 200 and the 422, and restores the config to empty
// before exiting — it must never leave test state behind.
//
// Usage: mcp_config_check [port] [--keep]
//   Run with JARVIS_HOME pointing at a home that holds a valid api_token
//   (`~/.jarvis-forge` by default). Exit code 0 means every assertion passed.

let args = CommandLine.arguments
let port = args.count > 1 && !args[1].hasPrefix("-") ? args[1] : "8765"
let keep = args.contains("--keep")
// APIClient reads the port from the environment at first use; set it before any
// request. The token file comes from JARVIS_HOME, exactly as in the app.
setenv("JARVIS_BACKEND_PORT", port, 1)

let api = APIClient.shared
var failures = 0

func check(_ ok: Bool, _ label: String, _ detail: String = "") {
    print((ok ? "PASS" : "FAIL") + ": " + label + (detail.isEmpty ? "" : " — " + detail))
    if !ok { failures += 1 }
}

func describe(_ error: Error) -> String {
    if case APIError.httpError(let code, let detail) = error {
        return "HTTP \(code): \(detail)"
    }
    return error.localizedDescription
}

/// Run one async check and wait for it, pumping the main run loop so the
/// URLSession callbacks land (a plain `sem.wait()` deadlocks on the main queue
/// in an NSApplication-free tool only if the session uses the main queue; it
/// does not, but pumping keeps this safe and lets any MainActor work run).
func run(_ body: @escaping () async -> Void) {
    let done = DispatchSemaphore(value: 0)
    Task {
        await body()
        done.signal()
    }
    while done.wait(timeout: .now()) == .timedOut {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
    }
}

print("--- initial status")
run {
    do {
        let before = try await api.mcpStatus(probe: false)
        print("before: state=\(before.connection.state) "
              + "configured=\(before.connection.configured == true) "
              + "url=\(before.connection.url ?? "")")
    } catch {
        check(false, "read initial status", describe(error))
    }
}

print("--- PUT valid endpoint")
run {
    do {
        let result = try await api.mcpConfigure(
            url: "http://127.0.0.1:9/mcp",
            allowedTools: "send_email, make_task, send_email",
            name: "swift-check")
        let allowed = result.allowed_tools ?? []
        check(result.saved == true, "PUT saves", "saved=\(String(describing: result.saved))")
        check(allowed == ["send_email", "make_task"],
              "server parsed + de-duplicated the allowlist",
              allowed.joined(separator: ","))
        check(result.config?.url == "http://127.0.0.1:9/mcp",
              "response echoes the saved URL",
              result.config?.url ?? "<nil>")
    } catch {
        check(false, "PUT valid endpoint", describe(error))
    }
}

print("--- status reflects the save")
run {
    do {
        let after = try await api.mcpStatus(probe: false)
        check(after.connection.configured == true,
              "status reflects the saved config", "state=\(after.connection.state)")
        check((after.connection.allowed_tools ?? []) == ["send_email", "make_task"],
              "status reports the saved allowlist",
              (after.connection.allowed_tools ?? []).joined(separator: ","))
    } catch {
        check(false, "status after save", describe(error))
    }
}

print("--- PUT rejects ftp://")
run {
    do {
        _ = try await api.mcpConfigure(url: "ftp://x", allowedTools: "send_email", name: "")
        check(false, "PUT rejects ftp://", "the server accepted it")
    } catch let error as APIError {
        if case .httpError(let code, let detail) = error {
            check(code == 422, "PUT rejects ftp:// with 422", "got \(code)")
            check(detail.contains("http://") && detail.contains("ftp://x"),
                  "422 detail is the server's own sentence", detail)
        } else {
            check(false, "PUT rejects ftp:// with an http error", describe(error))
        }
    } catch {
        check(false, "PUT rejects ftp://", describe(error))
    }
}

if !keep {
    print("--- restore config to empty")
    run {
        do {
            // A whitespace name is the documented way to remove it: the route
            // treats "" as "leave alone", but a non-empty string reaches
            // save_mcp_config, which strips it and drops the key.
            let result = try await api.mcpConfigure(url: "", allowedTools: "", name: " ")
            check(result.saved == true, "restored the config to empty")
        } catch {
            check(false, "restore config to empty", describe(error))
        }
    }
}

print(failures == 0 ? "RESULT: all checks passed" : "RESULT: \(failures) check(s) failed")
exit(failures == 0 ? 0 : 1)