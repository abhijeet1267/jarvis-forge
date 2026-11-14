import Foundation

// Isolated, production-safe integration harness for BackendManager's connection
// lifecycle. Drives the REAL BackendManager (real healthFetcher, real state
// machine, real URLSession) against a REAL local HTTP backend (server.py) on a
// chosen port, via the internal JARVIS_BACKEND_PORT seam (production default
// stays 8765; the server runs on an ephemeral port so it never touches the live
// backend). The backend's behavior (200 / 401 / dropped) is toggled by writing
// a mode file between phases. No mock fetcher, no stub client.
//
// Top-level async (main.swift) — no blocking semaphore, so the main actor can
// run the async work.

let BASE = "/tmp/bm_harness"
let MODEF = BASE + "/mode"
let PORTF = BASE + "/port"
let ROOTF = BASE + "/source_root"

func readModeFile(_ p: String) -> String {
    (try? String(contentsOfFile: p, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}
func writeMode(_ m: String) { try? m.write(toFile: MODEF, atomically: true, encoding: .utf8) }
func hits() -> Int { Int(readModeFile(BASE + "/hits")) ?? 0 }

var port = 0
for _ in 0..<40 {
    if let v = Int(readModeFile(PORTF)), v > 0 { port = v; break }
    Thread.sleep(forTimeInterval: 0.1)
}
if port == 0 { print("FAIL: backend harness server did not start"); exit(1) }

let scratch = NSTemporaryDirectory() + "/bm_home_" + String(Int.random(in: 0..<999999))
try? FileManager.default.createDirectory(atPath: scratch, withIntermediateDirectories: true)
try? "harness-test-token".write(toFile: scratch + "/api_token", atomically: true, encoding: .utf8)
setenv("JARVIS_BACKEND_PORT", String(port), 1)
setenv("JARVIS_HOME", scratch, 1)
// The fake backend reports this path as its source_root. The driver's own
// findProjectDir() must land on the same value for the "same checkout"
// cases, so it is written before the manager is ever asked to connect.
let expectedRoot = NSHomeDirectory() + "/AppData/vantage/jarvis-forge"
try? expectedRoot.write(toFile: ROOTF, atomically: true, encoding: .utf8)
print("harness: backend on \(port)")

func describe(_ s: BackendManager.State) -> String {
    switch s {
    case .unknown: return "unknown"
    case .checking: return "checking"
    case .running: return "running"
    case .starting: return "starting"
    case .stopped(let r): return "stopped(\(r))"
    case .error(let m): return "error(\(m))"
    }
}
func isAuthError(_ s: BackendManager.State) -> Bool {
    if case .error(let m) = s { return m.lowercased().contains("authoriz") }
    return false
}
func isNetError(_ s: BackendManager.State) -> Bool {
    if case .error(let m) = s { return m.lowercased().contains("unreachable") }
    return false
}
func sleepMS(_ ms: Int) async { try? await Task.sleep(nanoseconds: UInt64(ms) * 1_000_000) }

var lines: [String] = []
var allOK = true
func check(_ label: String, _ cond: Bool, _ detail: String) {
    lines.append("\(cond ? "PASS" : "FAIL")  \(label)   [\(!cond ? detail : "")]")
    if !cond { allOK = false }
}

let m = BackendManager()
m.pollInterval = 0.4
writeMode("up")
await sleepMS(300)

// 1. Healthy authenticated connection
await m.connect()
check("connect -> running", m.state == .running, describe(m.state))
check("health decoded (status ok)", (m.health?.status) == "ok", "\(String(describing: m.health))")
check("poll task created", m.healthPollTask != nil, "nil")

// 2. Poll loop flowing (baseline rate over 1.2s)
let h0 = hits()
await sleepMS(1200)
let baseRate = hits() - h0
check("baseline poll rate (2..4 polls/1.2s)", baseRate >= 2 && baseRate <= 4, "rate=\(baseRate)")

// 3. Backend down -> truthful error state
writeMode("down")
var waited = 0
while waited < 24 && !describe(m.state).hasPrefix("error") {
    await sleepMS(250); waited += 1
}
let downDesc = describe(m.state)
check("down -> error state", downDesc.hasPrefix("error"), downDesc)
check("down -> network/unreachable message (not auth)", isNetError(m.state), downDesc)

// 4. Recovery -> running again + poll loop alive
writeMode("up")
waited = 0
while waited < 24 && m.state != .running {
    await sleepMS(250); waited += 1
}
check("recovery -> running", m.state == .running, describe(m.state))
let hr0 = hits()
await sleepMS(1200)
let recRate = hits() - hr0
check("recovery -> poll loop alive (2..4 polls/1.2s)", recRate >= 2 && recRate <= 4, "rate=\(recRate)")

// 5. Repeated connect must NOT double the poll loop
await m.connect()
await m.connect()
let hd0 = hits()
await sleepMS(1200)
let repRate = hits() - hd0
check("repeat connect -> poll rate NOT doubled (2..5, not ~8)", repRate >= 2 && repRate <= 5, "rate=\(repRate)")
check("repeat connect -> still running", m.state == .running, describe(m.state))

// 6. Auth failure distinguished from network error
writeMode("401")
waited = 0
while waited < 24 && !isAuthError(m.state) {
    await sleepMS(250); waited += 1
}
check("401 -> authorization error (not 'unreachable')", isAuthError(m.state), describe(m.state))

// 7. Identity: a current build from this checkout -> running, no warning
writeMode("up")
waited = 0
while waited < 24 && m.state != .running {
    await sleepMS(250); waited += 1
}
await m.refresh()
check("identity current -> running", m.state == .running, describe(m.state))
check("identity current -> identity captured",
      m.identity?.service == "vantage-backend" && m.identity?.source_root == expectedRoot,
      "identity=\(String(describing: m.identity?.source_root))")
check("identity current -> no warning", m.warning == nil && m.stale == false,
      "warning=\(String(describing: m.warning)) stale=\(m.stale)")

// 8. Stale build -> still running (usable) + warning + stale flag
writeMode("stale")
await m.refresh()
check("stale -> still running (usable, not error)", m.state == .running, describe(m.state))
check("stale -> stale flag set", m.stale, "stale=\(m.stale)")
check("stale -> warning names the restart",
      (m.warning ?? "").contains("older than the code on disk")
        && (m.warning ?? "").contains("backend_lifecycle.py restart"),
      "warning=\(String(describing: m.warning))")
// A stale backend must not block the app: a newer healthy poll still works,
// and the poll loop must still be alive.
let hs0 = hits()
await sleepMS(1200)
check("stale -> poll loop still alive (2..4 polls/1.2s)", (hits() - hs0) >= 2,
      "rate=\(hits() - hs0)")

// 9. Foreign folder -> error naming the folder; connect() must not spawn
try? "/tmp/somewhere-else/jarvis-forge".write(toFile: ROOTF, atomically: true, encoding: .utf8)
writeMode("foreign-folder")
await m.refresh()
guard case .error(let foreignMsg) = m.state else {
    check("foreign folder -> error", false, describe(m.state))
    exit(1)
}
check("foreign folder -> error", true, "")
check("foreign folder -> names the path",
      foreignMsg.contains("/tmp/somewhere-else/jarvis-forge")
        && foreignMsg.contains("different folder"),
      foreignMsg)
check("foreign folder -> no warning", m.warning == nil, "warning=\(String(describing: m.warning))")
// connect() while the port is owned by the foreign backend: no second process
// may be started (the state must not become .starting, and it must stay an error).
await m.connect()
let afterConnect = describe(m.state)
check("foreign folder -> connect() starts nothing (stays error, never starting)",
      afterConnect.hasPrefix("error"), afterConnect)

// Restore this checkout's root for the remaining cases.
try? expectedRoot.write(toFile: ROOTF, atomically: true, encoding: .utf8)

// 10. Foreign service -> error, never healthy
writeMode("foreign-service")
await m.refresh()
guard case .error(let svcMsg) = m.state else {
    check("foreign service -> error", false, describe(m.state))
    exit(1)
}
check("foreign service -> error names the service",
      svcMsg.contains("some-other-service"), svcMsg)

// 11. Old build (no /health/identity, 404) -> error, NOT healthy
writeMode("old")
await m.refresh()
guard case .error(let oldMsg) = m.state else {
    check("old build -> error (not healthy)", false, describe(m.state))
    exit(1)
}
check("old build -> error (not healthy)", true, "")
check("old build -> actionable restart message",
      oldMsg.contains("older Vantage build") && oldMsg.contains("backend_lifecycle.py restart"),
      oldMsg)

// 12. Clean shutdown / cancellation
writeMode("up")
m.stopHealthPolling()
check("shutdown -> poll task cleared", m.healthPollTask == nil, "not nil")

print("\n===== BACKEND MANAGER LIFECYCLE RESULTS =====")
for l in lines { print(l) }
print(allOK ? "===== ALL CHECKS PASSED =====" : "===== SOME CHECKS FAILED =====")
try? FileManager.default.removeItem(atPath: scratch)
exit(allOK ? 0 : 1)
