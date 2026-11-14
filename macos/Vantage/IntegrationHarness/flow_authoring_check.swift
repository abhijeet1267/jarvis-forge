import Foundation
import Carbon
var failures = 0
func check(_ n: String, _ ok: Bool, _ d: @autoclosure () -> String = "") {
    print("\(ok ? "PASS" : "FAIL")  \(n)\(ok || d().isEmpty ? "" : "  [\(d())]")")
    if !ok { failures += 1 }
}
func dir() -> URL {
    let u = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("ah-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true); return u
}
func st(_ k: MacActionKind, _ c: (inout StoredArguments) -> Void = { _ in }) -> FlowStep {
    var a = StoredArguments(); c(&a); return FlowStep(actionID: k.id, arguments: a)
}
let apps = ["Safari", "Finder", "Calculator"]
let d = dir(); defer { try? FileManager.default.removeItem(at: d) }
let store = FlowStore(directory: d)
let authoring = FlowAuthoring(applications: apps)
let resolver = AnywhereCommand(applications: apps, store: store)

Task { @MainActor in
    // --- authoring: draft, never save/run
    if case .draft(let f) = authoring.interpret(
        "Create a Start Workday flow that opens Safari, Finder and my Documents folder, then sets volume to 25 percent", store: store) {
        check("draft name", f.name == "Start Workday", f.name)
        check("draft steps mapped", f.steps.map(\.actionID) == ["open_application", "open_application", "open_folder", "set_volume"], "\(f.steps.map(\.actionID))")
        check("draft percent", f.steps.last?.arguments.percent == 25)
        check("draft not auto-saved", store.flows.isEmpty, "\(store.flows.count)")
    } else { check("draft produced", false) }

    if case .incompleteDraft(let f, let p) = authoring.interpret("create a flow that opens Safari and then makes me a sandwich", store: store) {
        check("partial draft keeps valid steps", f.steps.count == 1, "\(f.steps.count)")
        check("partial draft reports problems", !p.isEmpty)
    } else { check("partial draft", false) }

    if case .incompleteDraft(let f, let p) = authoring.interpret("create a flow that does something useful", store: store) {
        check("unmappable draft has no steps", f.steps.isEmpty)
        check("unmappable draft says so", p.contains { $0.contains("No step") }, "\(p)")
    } else { check("unmappable draft", false) }

    // --- three intents
    _ = store.save(Flow(name: "Safari", steps: [st(.openApplication) { $0.application = "Safari" }]))
    func intent(_ t: String) -> String {
        switch authoring.interpret(t, store: store) {
        case .draft: return "draft"; case .incompleteDraft: return "incompleteDraft"
        case .run: return "run"; case .describe: return "describe"
        case .unknownFlow: return "unknownFlow"; case .notAFlowRequest: return "other" }
    }
    check("create is a draft", intent("create a flow that opens Finder") == "draft", intent("create a flow that opens Finder"))
    check("run resolves", intent("run my Safari flow") == "run", intent("run my Safari flow"))
    check("describe resolves", intent("what does my Safari flow do") == "describe", intent("what does my Safari flow do"))
    check("show-what resolves", intent("show me what the Safari flow does") == "describe", intent("show me what the Safari flow does"))
    check("unknown flow names known ones", {
        if case .unknownFlow(let m) = authoring.interpret("run my Lunch flow", store: store) { return m.contains("Safari") }
        return false }())
    check("plain command is not a draft", intent("Open Finder") == "other", intent("Open Finder"))

    // --- palette resolver
    func outcomeKind(_ t: String) -> String {
        switch resolver.resolve(t).outcome {
        case .action(let k, _): return "action:\(k.id)"
        case .plan: return "plan"; case .runFlow: return "runFlow"
        case .describeFlow: return "describeFlow"; case .draftFlow: return "draftFlow"
        case .listFlows: return "listFlows"; case .explanation: return "explanation"
        case .clarification: return "clarification"; case .conversation: return "conversation"
        case .flowProblem: return "flowProblem"
        // Phase 3 outcomes: named here so a new one cannot silently fall
        // through to a stale expectation.
        case .capture: return "capture"; case .showInbox: return "showInbox"
        case .briefing: return "briefing"; case .openDocuments: return "openDocuments"
        case .documentQuestion: return "documentQuestion" }
    }
    check("palette open Safari", outcomeKind("Open Finder") == "action:open_application", outcomeKind("Open Finder"))
    check("palette volume", outcomeKind("set the volume to 30 percent") == "action:set_volume", outcomeKind("set the volume to 30 percent"))
    check("palette runs saved flow", outcomeKind("run my Safari flow") == "runFlow", outcomeKind("run my Safari flow"))
    check("palette question not executed", outcomeKind("how do I change the volume?") == "explanation", outcomeKind("how do I change the volume?"))
    check("palette conversation left to window", outcomeKind("write me a poem") == "conversation", outcomeKind("write me a poem"))
    check("palette close needs confirmation", resolver.resolve("close the current window").needsConfirmation)
    check("palette open needs no confirmation", !resolver.resolve("open Finder").needsConfirmation)
    check("palette drafts a flow", outcomeKind("create a flow that opens Safari") == "draftFlow", outcomeKind("create a flow that opens Safari"))

    // --- suggestions are real
    let sugg = resolver.suggestions(recent: ["Open Calculator"], limit: 8)
    check("recent appears in suggestions", sugg.contains { $0.text == "Open Calculator" })
    check("every suggestion resolves off the dead-end path",
          sugg.allSatisfy { if case .flowProblem = resolver.resolve($0.text).outcome { return false }; return true })

    // --- shortcut spec
    check("default is option-space", ShortcutSpec.defaultShortcut.displayString == "⌥Space", ShortcutSpec.defaultShortcut.displayString)
    let encoded = try? JSONEncoder().encode(ShortcutSpec(keyCode: 40, modifiers: UInt32(cmdKey) | UInt32(shiftKey)))
    check("shortcut round-trips", (try? JSONDecoder().decode(ShortcutSpec.self, from: encoded!)) == ShortcutSpec(keyCode: 40, modifiers: UInt32(cmdKey) | UInt32(shiftKey)))

    print("\n\(failures == 0 ? "ALL AUTHORING/PALETTE CHECKS PASSED" : "\(failures) FAILED")")
    exit(failures == 0 ? 0 : 1)
}
RunLoop.main.run()
