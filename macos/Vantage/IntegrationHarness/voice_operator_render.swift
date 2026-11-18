#!/usr/bin/env swift
import AppKit
import SwiftUI

// Offscreen render of VoiceOperatorV2View, including its populated states.
//
// The empty state proves layout; these fixtures prove the two states a person
// actually sees after using it: a conversation, and a parked desktop plan
// awaiting approval. Rendering them is the only way to see the approval card's
// real layout without granting this process accessibility and driving a live
// mission -- which is a different, permission-gated test.
//
// This is NOT acceptance of the controls. It cannot press Approve; it proves
// the card lays out and draws.

let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/voice-op-render"
try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

func render<V: View>(_ view: V, name: String, size: NSSize, appearance: NSAppearance) -> Bool {
    let hosting = NSHostingView(rootView: view)
    hosting.frame = NSRect(origin: .zero, size: size)
    hosting.appearance = appearance
    hosting.layoutSubtreeIfNeeded()
    let fitting = hosting.fittingSize
    let final = NSSize(width: max(size.width, fitting.width),
                       height: max(size.height, fitting.height))
    hosting.frame = NSRect(origin: .zero, size: final)
    hosting.layoutSubtreeIfNeeded()

    guard let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else {
        print("FAIL \(name): bitmap"); return false
    }
    hosting.cacheDisplay(in: hosting.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else {
        print("FAIL \(name): encode"); return false
    }
    let path = "\(outDir)/\(name).png"
    do { try png.write(to: URL(fileURLWithPath: path)) }
    catch { print("FAIL \(name): \(error)"); return false }
    let blank = isBlank(rep)
    print("\(blank ? "BLANK " : "OK   ") \(name) \(Int(final.width))x\(Int(final.height))")
    return !blank
}

func isBlank(_ rep: NSBitmapImageRep) -> Bool {
    let w = rep.pixelsWide, h = rep.pixelsHigh
    guard w > 0, h > 0 else { return true }
    var first: NSColor?
    for y in stride(from: 0, to: h, by: max(1, h / 40)) {
        for x in stride(from: 0, to: w, by: max(1, w / 40)) {
            guard let c = rep.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else { continue }
            if let f = first {
                let d = abs(c.redComponent - f.redComponent) + abs(c.greenComponent - f.greenComponent)
                    + abs(c.blueComponent - f.blueComponent)
                if d > 0.02 { return false }
            } else { first = c }
        }
    }
    return true
}

// MARK: - Fixtures (decoded through the real model types)

func message(_ text: String, _ role: ChatMessage.Role) -> ChatMessage {
    ChatMessage(text: text, role: role, meta: .init())
}

func pendingApproval() -> PendingDesktopApproval? {
    let stepJSON = """
    [{"step_id":"S1","title":"type into the note","tool":"desktop_type_text",\
    "params":{"text":"Hello from Vantage"},"status":"pending","detail":"",\
    "satisfies":"","idempotency_key":"","depends_on":[]}]
    """
    let missionJSON = """
    {"mission_id":"MISSION-DEMO","request":"open TextEdit and type a note",\
    "outcome":null,"state":"awaiting_approval","verdict":"unknown",\
    "reason":"waiting for approval before any action ran","conditions":[],\
    "steps":\(stepJSON),"condition_summary":{},"plan_revision":0}
    """
    guard let missionData = missionJSON.data(using: .utf8),
          let mission = try? JSONDecoder().decode(APIModels.Mission.self, from: missionData)
    else { return nil }

    let steps = [
        PendingDesktopApproval.Step(
            id: "S1", title: "type into the note", status: "pending",
            tool: "desktop_type_text", detail: "",
            paramsSummary: "text: Hello from Vantage")
    ]
    return PendingDesktopApproval(
        mission: mission, missionId: "MISSION-DEMO", confirmationId: "CONF-DEMO",
        steps: steps,
        summary: "This will type into the frontmost window of TextEdit.")
}

let conversation: [ChatMessage] = [
    message("Open TextEdit and type Hello from Vantage.", .user),
    message("I planned two steps: open TextEdit, then type into its window. "
            + "The first one needs your approval before it runs.", .assistant),
]

var ok = true
let size = NSSize(width: 900, height: 640)
let aqua = NSAppearance(named: .aqua)!
let dark = NSAppearance(named: .darkAqua)!

ok = render(VoiceOperatorV2View(), name: "empty-light", size: size, appearance: aqua) && ok
ok = render(VoiceOperatorV2View(), name: "empty-dark", size: size, appearance: dark) && ok
ok = render(VoiceOperatorV2View(previewMessages: conversation),
            name: "conversation-light", size: size, appearance: aqua) && ok
if let approval = pendingApproval() {
    ok = render(VoiceOperatorV2View(previewMessages: conversation, previewApproval: approval),
                name: "approval-light", size: NSSize(width: 900, height: 760), appearance: aqua) && ok
    ok = render(VoiceOperatorV2View(previewMessages: conversation, previewApproval: approval),
                name: "approval-dark", size: NSSize(width: 900, height: 760), appearance: dark) && ok
} else {
    print("FAIL: could not build the approval fixture")
    ok = false
}

exit(ok ? 0 : 1)