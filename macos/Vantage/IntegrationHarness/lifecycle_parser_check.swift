import Foundation

// Parser checks for BackendLifecycle against the REAL script's status output.
//
// Driven by run_lifecycle_check.sh, which runs
// `scripts/backend_lifecycle.py status --port N --json` against fixture
// listeners it controls and passes the resulting files as arguments:
//
//   lifecycle_parser_check.swift ours.json legacy.json foreign.json other.json free.json
//
// Each file is decoded with the real Codable types (BackendLifecycle.Status)
// and the mapped state is compared with what the script wrote. Also proves
// that a refused action is surfaced, and that the busy guard refuses a second
// concurrent operation rather than launching two processes.

func fail(_ message: String) -> Never {
    print("FAIL  \(message)")
    exit(1)
}

func load(_ path: String) -> BackendLifecycle.Status {
    guard let data = FileManager.default.contents(atPath: path) else {
        fail("cannot read \(path)")
    }
    do {
        return try JSONDecoder().decode(BackendLifecycle.Status.self, from: data)
    } catch {
        fail("decode \(path): \(error)")
    }
}

let args = Array(CommandLine.arguments.dropFirst())
guard args.count == 5 else {
    fail("usage: lifecycle_parser_check <ours> <legacy> <foreign> <other> <free>")
}
let files = Dictionary(uniqueKeysWithValues: zip(["ours", "legacy", "foreign", "other", "free"], args))

var failures = 0
func expect(_ label: String, _ condition: Bool, _ detail: @autoclosure () -> String) {
    if condition {
        print("PASS  \(label)")
    } else {
        failures += 1
        print("FAIL  \(label)   [\(detail())]")
    }
}

// --- The five states, from bytes the real script produced --------------------

let ours = load(files["ours"]!)
expect("ours -> kind .ours", ours.kind == .ours, "kind=\(ours.kind)")
expect("ours -> identity decodes", ours.identity?.service == "vantage-backend", "identity=\(String(describing: ours.identity))")
expect("ours -> instance id retained", (ours.instanceID ?? "").hasPrefix("BKT-"), "instance=\(String(describing: ours.instanceID))")
expect("ours -> stale is a real bool", ours.stale != nil, "stale=nil")
expect("ours -> safe_to_restart", ours.safe_to_restart, "false")

let legacy = load(files["legacy"]!)
expect("legacy -> kind .legacy", legacy.kind == .legacy, "kind=\(legacy.kind)")
expect("legacy -> migratable", legacy.migratable, "false")
expect("legacy -> not safe_to_restart", !legacy.safe_to_restart, "true")
expect("legacy -> stale unknown (nil, not false)", legacy.stale == nil, "stale=\(String(describing: legacy.stale))")
// The script emits `identity: {}` for a listener that cannot answer the
// route, so the struct decodes with every field absent -- not as a throw.
expect("legacy -> identity carries no claims",
       legacy.identity?.service == nil && legacy.identity?.source_root == nil,
       "identity=\(String(describing: legacy.identity))")
expect("legacy -> evidence reason carries the OS facts",
       (legacy.legacy?.reason ?? "").contains("this checkout"), "reason=\(legacy.legacyReason)")
expect("legacy -> evidence pid matches listener", legacy.legacy?.pid == legacy.pid, "evidence=\(String(describing: legacy.legacy?.pid)) listener=\(String(describing: legacy.pid))")

let foreign = load(files["foreign"]!)
expect("foreign -> kind .foreign", foreign.kind == .foreign, "kind=\(foreign.kind)")
expect("foreign -> not migratable / not restartable", !foreign.migratable && !foreign.safe_to_restart, "")
// The fixture runs from outside the checkout, so the cwd check (the decisive
// one) is what must be cited; the exact wording belongs to the script.
expect("foreign -> refusal reason cites the decisive OS evidence",
       (foreign.legacy?.reason ?? "").contains("outside"), "reason=\(foreign.legacyReason)")

let other = load(files["other"]!)
expect("other checkout -> kind .otherVantageCheckout", other.kind == .otherVantageCheckout, "kind=\(other.kind)")
expect("other checkout -> source_root retained", (other.identity?.source_root ?? "").contains("other-checkout"), "root=\(String(describing: other.identity?.source_root))")
expect("other checkout -> not actionable", !other.migratable && !other.safe_to_restart, "")

let free = load(files["free"]!)
expect("free -> kind .free", free.kind == .free, "kind=\(free.kind)")
expect("free -> no pid and no identity claims",
       free.pid == nil && free.instanceID == nil, "pid=\(String(describing: free.pid))")

// --- Unknown state strings are never actionable ------------------------------

let unknownData = Data(#"{"port":1,"pid":null,"state":"something-new","identity":null,"code":{},"stale":null,"legacy":null,"safe_to_restart":false,"migratable":false}"#.utf8)
if let unknown = try? JSONDecoder().decode(BackendLifecycle.Status.self, from: unknownData) {
    expect("unknown state string -> .unknown (never actionable)", unknown.kind == .unknown, "kind=\(unknown.kind)")
} else {
    failures += 1
    print("FAIL  unknown state string must decode, not throw")
}

// --- Refusal helper names the port, the evidence, and the manual command -----

let refusal = BackendManager.refusal(for: foreign, port: 4321)
expect("refusal names the port", refusal.contains("4321"), refusal)
expect("refusal says it will not stop an unproven process", refusal.contains("will not stop"), refusal)
expect("refusal carries the script's reason", refusal.contains(foreign.legacyReason), refusal)
expect("refusal gives the manual status command", refusal.contains("backend_lifecycle.py status"), refusal)

// --- Concurrent invocation: a second call is refused, not a second process ---

let repo = BackendManager.resolvedProjectDir()
let fixturePort = Int(ProcessInfo.processInfo.environment["FIXTURE_PORT"] ?? "0") ?? 0
if fixturePort > 0 {
    let lifecycle = BackendLifecycle()
    // Two overlapping operations; at most one may run, the other gets .busy.
    await withTaskGroup(of: Bool.self) { group in
        for _ in 0..<2 {
            group.addTask {
                do {
                    _ = try await lifecycle.perform(.start, port: fixturePort, repo: repo)
                    return true
                } catch BackendLifecycle.LifecycleError.busy {
                    return false
                } catch {
                    return true
                }
            }
        }
        var ran = 0
        var busy = 0
        for await didRun in group {
            if didRun { ran += 1 } else { busy += 1 }
        }
        expect("concurrent action -> exactly one runs, the other is refused as busy",
               busy == 1 && ran == 1, "busy=\(busy) ran=\(ran)")
    }
}

print(failures == 0 ? "===== PARSER CHECKS PASSED =====" : "===== PARSER CHECKS FAILED =====")
exit(failures == 0 ? 0 : 1)