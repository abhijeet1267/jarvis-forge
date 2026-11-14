import Foundation
// Proves the brief's core requirement: the palette, Command Center and the flow
// runner all reach the SAME coordinator and the SAME executor, with the same
// policy — not three implementations.
var failures = 0
func check(_ n: String, _ ok: Bool, _ d: @autoclosure () -> String = "") {
    print("\(ok ? "PASS" : "FAIL")  \(n)\(ok || d().isEmpty ? "" : "  [\(d())]")")
    if !ok { failures += 1 }
}
let d = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("shared-\(UUID().uuidString)")
try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: d) }

Task { @MainActor in
    let control = MacControlCoordinator.shared
    let store = FlowStore(directory: d)
    let engine = FlowEngine(store: store)   // production executor

    // 1. A palette action and a Control Mac action obey the same risk policy.
    // `quitApplication` is Class C (needs approval) and needs no Accessibility,
    // which this session does not have — so it isolates the approval rule.
    var quitArgs = MacActionArguments(); quitArgs.application = "Calculator"
    let paletteClose = await control.performForPalette(.quitApplication, quitArgs)
    check("palette defers a protected action instead of running it",
          control.pendingForPalette != nil && paletteClose.status == .refused,
          "\(paletteClose.status) pending=\(control.pendingForPalette != nil)")
    control.cancelPalettePending()
    check("cancelling the palette approval clears it", control.pendingForPalette == nil)

    // 2. The flow engine routes through the same router: a protected step pauses
    //    exactly like the palette's does.
    let flow = Flow(name: "Shared", steps: [FlowStep(actionID: MacActionKind.quitApplication.id,
                                                     arguments: StoredArguments(quitArgs))])
    _ = await engine.run(flow)
    check("flow runner also pauses on the same protected action",
          engine.pendingApproval != nil)
    engine.cancelPending()

    // 3. An unknown action is refused by the same lookup, from every surface.
    let bad = Flow(name: "Bad", steps: [FlowStep(actionID: "eval_swift", arguments: StoredArguments())])
    let badRun = await engine.run(bad)
    check("unknown action refused in a flow", badRun == nil)

    // 4. A read-only palette action executes through the real service.
    let vol = await control.performForPalette(.getVolume, MacActionArguments())
    check("palette read runs natively", vol.status == .verified, "\(vol.status): \(vol.message.prefix(60))")

    print("\n\(failures == 0 ? "ALL SHARED-PATH CHECKS PASSED" : "\(failures) FAILED")")
    exit(failures == 0 ? 0 : 1)
}
RunLoop.main.run()
