import Foundation

@MainActor
final class Rec: MacActionExecutor {
    enum Reply { case ok, unverified, failed }
    var replies: [String: Reply]; var executed: [String] = []
    var onExecute: ((MacActionRequest) -> Void)?
    init(_ r: [String: Reply] = [:]) { replies = r }
    func execute(_ req: MacActionRequest,
                 cancellation: MacControlService.Cancellation?) async -> MacActionResult {
        executed.append(req.kind.id); onExecute?(req)
        switch replies[req.kind.id] ?? .ok {
        case .ok: return .verified(req.kind, "ok")
        case .unverified: return .unverified(req.kind, "asked")
        case .failed: return .failed(req.kind, "boom")
        }
    }
}

var failures = 0
func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
    print("\(ok ? "PASS" : "FAIL")  \(name)\(ok || detail().isEmpty ? "" : "  [\(detail())]")")
    if !ok { failures += 1 }
}
let granted = MacPermissionStatus(accessibilityGranted: true)
func dir() -> URL {
    let u = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("fh-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
    return u
}
func st(_ k: MacActionKind, _ c: (inout StoredArguments) -> Void = { _ in }, cont: Bool = false) -> FlowStep {
    var a = StoredArguments(); c(&a)
    return FlowStep(actionID: k.id, arguments: a, continueOnFailure: cont)
}

let task = Task { @MainActor in
    // 1. Ordering
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec()
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        let f = Flow(name: "T", steps: [st(.frontmostApp), st(.systemInfo), st(.listRunningApps)])
        let run = await eng.run(f, permissions: granted)
        check("steps run in order", ex.executed == ["frontmost_app", "system_info", "list_running_apps"], "\(ex.executed)")
        check("ordered run completes", run?.state == .completed, "\(String(describing: run?.state))")
    }
    // 2. Required failure stops dependents
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec(["open_application": .failed])
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        let f = Flow(name: "T", steps: [st(.openApplication) { $0.application = "Safari" },
                                       st(.setVolume) { $0.percent = 30 }])
        let run = await eng.run(f, permissions: granted)
        check("failure stops dependents", ex.executed == ["open_application"], "\(ex.executed)")
        check("failed run is .failed", run?.state == .failed, "\(String(describing: run?.state))")
    }
    // 3. continue policy
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec(["frontmost_app": .failed])
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        let f = Flow(name: "T", steps: [st(.frontmostApp, cont: true), st(.systemInfo)],
                     failurePolicy: .continueIndependent)
        let run = await eng.run(f, permissions: granted)
        check("marked-safe failure continues", ex.executed == ["frontmost_app", "system_info"], "\(ex.executed)")
        check("continue run records errors", run?.state == .completedWithErrors, "\(String(describing: run?.state))")
    }
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec(["frontmost_app": .failed])
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        let f = Flow(name: "T", steps: [st(.frontmostApp), st(.systemInfo)], failurePolicy: .continueIndependent)
        _ = await eng.run(f, permissions: granted)
        check("unmarked failure still stops", ex.executed == ["frontmost_app"], "\(ex.executed)")
    }
    // 4. unknown action never reaches executor
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec()
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        let bad = FlowStep(actionID: "run_shell", arguments: StoredArguments())
        let run = await eng.run(Flow(name: "T", steps: [bad]), permissions: granted)
        check("unknown action blocked", ex.executed.isEmpty, "\(ex.executed)")
        check("unknown action records no success", run == nil, "\(String(describing: run))")
    }
    // 5. permission blocks
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec()
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        let run = await eng.run(Flow(name: "T", steps: [st(.minimizeWindow)]),
                                permissions: MacPermissionStatus(accessibilityGranted: false))
        check("missing permission blocks all steps", ex.executed.isEmpty, "\(ex.executed)")
        check("blocked run records nothing", run == nil)
    }
    // 6. approval
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec()
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        let f = Flow(name: "T", steps: [st(.openApplication) { $0.application = "Safari" }, st(.closeWindow)])
        _ = await eng.run(f, permissions: granted)
        check("protected step pauses", ex.executed == ["open_application"], "\(ex.executed)")
        check("approval is pending", eng.pendingApproval?.nextIndex == 1)
        let run = await eng.approvePending()
        check("approve runs the step", ex.executed == ["open_application", "close_window"], "\(ex.executed)")
        check("approved run completes", run?.state == .completed, "\(String(describing: run?.state))")
    }
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec()
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        _ = await eng.run(Flow(name: "T", steps: [st(.closeWindow)]), permissions: granted)
        eng.cancelPending()
        check("declining runs nothing", ex.executed.isEmpty, "\(ex.executed)")
    }
    // 7. cancellation
    do {
        let d = dir(); let store = FlowStore(directory: d); defer { try? FileManager.default.removeItem(at: d) }
        let ex = Rec()
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        ex.onExecute = { _ in eng.cancel() }
        let run = await eng.run(Flow(name: "T", steps: [st(.frontmostApp), st(.systemInfo)]), permissions: granted)
        check("cancel stops unstarted steps", ex.executed == ["frontmost_app"], "\(ex.executed)")
        check("cancel is not completion", run?.state == .cancelled, "\(String(describing: run?.state))")
    }
    // 8. persistence + history survives delete
    do {
        let d = dir(); defer { try? FileManager.default.removeItem(at: d) }
        let store = FlowStore(directory: d)
        let ex = Rec()
        let eng = FlowEngine(store: store, router: MacActionRouter(permissions: { granted }), executor: ex)
        let f = Flow(name: "Persisted", steps: [st(.openFolder) { $0.path = "Downloads" }])
        _ = store.save(f)
        let reopened = FlowStore(directory: d)
        check("flow persists across store reopen", reopened.flows.first?.name == "Persisted",
              "\(reopened.flows.map(\.name))")
        _ = await eng.run(f, permissions: granted)
        _ = store.delete(f)
        let after = FlowStore(directory: d)
        check("history outlives flow deletion", after.flows.isEmpty && after.history.count == 1,
              "flows=\(after.flows.count) runs=\(after.history.count)")
    }
    // 9. corrupt file quarantined
    do {
        let d = dir(); defer { try? FileManager.default.removeItem(at: d) }
        try? "{ not json".write(to: d.appendingPathComponent("flows.json"), atomically: true, encoding: .utf8)
        let store = FlowStore(directory: d)
        check("corrupt file reports an error", store.loadError != nil)
        check("corrupt file quarantined", !FileManager.default.fileExists(atPath: d.appendingPathComponent("flows.json").path))
    }
    // 10. duplicate / reorder
    do {
        let d = dir(); defer { try? FileManager.default.removeItem(at: d) }
        let store = FlowStore(directory: d)
        var f = Flow(name: "R", steps: [st(.frontmostApp), st(.systemInfo)])
        _ = store.save(f)
        f.steps.swapAt(0, 1); _ = store.save(f)
        check("reorder persists", store.flows.first?.steps.first?.actionID == "system_info",
              "\(store.flows.first?.steps.first?.actionID ?? "nil")")
        _ = store.duplicate(f)
        check("duplicate makes a second flow", store.flows.count == 2, "\(store.flows.count)")
    }
    print("\n\(failures == 0 ? "ALL FLOW ENGINE CHECKS PASSED" : "\(failures) FAILED")")
    exit(failures == 0 ? 0 : 1)
}
_ = task
RunLoop.main.run()
