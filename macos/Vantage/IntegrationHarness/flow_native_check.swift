import Foundation
var failures = 0
func check(_ n: String, _ ok: Bool, _ d: @autoclosure () -> String = "") {
    print("\(ok ? "PASS" : "FAIL")  \(n)\(ok || d().isEmpty ? "" : "  [\(d())]")")
    if !ok { failures += 1 }
}
func args(_ c: (inout StoredArguments) -> Void) -> StoredArguments { var a = StoredArguments(); c(&a); return a }
let d = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("native-\(UUID().uuidString)")
try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: d) }

Task { @MainActor in
    let store = FlowStore(directory: d)
    // The REAL executor: NSWorkspace + CoreAudio. No fake.
    let engine = FlowEngine(store: store, router: MacActionRouter(), executor: NativeActionExecutor())
    let perms = MacPermissionInspector.current()
    print("accessibility granted: \(perms.accessibilityLabel)")

    // A flow of actions that work without Accessibility and are harmless.
    let flow = Flow(name: "Native Check", steps: [
        FlowStep(actionID: MacActionKind.frontmostApp.id),
        FlowStep(actionID: MacActionKind.getVolume.id),
        FlowStep(actionID: MacActionKind.openFolder.id, arguments: args { $0.path = "Downloads" }),
        FlowStep(actionID: MacActionKind.systemInfo.id),
    ])
    _ = store.save(flow)
    let run = await engine.run(flow)
    guard let run else { print("FAIL  flow did not run"); exit(1) }
    for o in run.outcomes {
        print("  \(o.resultStatus == .verified ? "ok  " : "?   ")\(o.title): \(o.message.prefix(120))")
    }
    check("all four steps verified", run.outcomes.filter { $0.resultStatus == .verified }.count == 4,
          "\(run.outcomes.map { "\($0.actionID)=\($0.status)" })")
    check("order preserved",
          run.outcomes.map(\.actionID) == ["frontmost_app", "get_volume", "open_folder", "system_info"],
          "\(run.outcomes.map(\.actionID))")
    check("run state completed", run.state == .completed, "\(run.state)")

    // Persistence of the real run, read back by a fresh store.
    let reopened = FlowStore(directory: d)
    check("run recorded to disk", reopened.history.count >= 1, "\(reopened.history.count)")
    check("history kept step outcomes", reopened.history.first?.outcomes.count == 4,
          "\(String(describing: reopened.history.first?.outcomes.count))")

    // A failing prerequisite must stop the dependent step — using a real action
    // whose target does not exist.
    let broken = Flow(name: "Broken", steps: [
        FlowStep(actionID: MacActionKind.openFolder.id,
                 arguments: args { $0.path = "/tmp/vantage-does-not-exist-\(UUID().uuidString)" }),
        FlowStep(actionID: MacActionKind.getVolume.id),
    ])
    let brokenRun = await engine.run(broken)
    check("missing folder fails truthfully", brokenRun?.state == .failed, "\(String(describing: brokenRun?.state))")
    check("dependent step did not run", brokenRun?.outcomes.count == 1,
          "\(String(describing: brokenRun?.outcomes.count))")

    // A window step with no Accessibility must refuse and never claim success.
    if !perms.permits(.accessibility) {
        let winRun = await engine.run(Flow(name: "Win", steps: [FlowStep(actionID: MacActionKind.minimizeWindow.id)]))
        check("window flow blocked without Accessibility", winRun == nil)
        check("blocked flow names the permission",
              (engine.live?.note ?? "").contains("Accessibility"), engine.live?.note ?? "")
    } else {
        print("SKIP  window checks (Accessibility is granted on this machine)")
    }

    print("\n\(failures == 0 ? "ALL NATIVE FLOW CHECKS PASSED" : "\(failures) FAILED")")
    exit(failures == 0 ? 0 : 1)
}
RunLoop.main.run()
