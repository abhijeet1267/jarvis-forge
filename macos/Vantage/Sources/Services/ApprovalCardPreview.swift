import AppKit
import SwiftUI

/// Renders `DesktopPlanApprovalCard` offscreen to PNGs, for visual acceptance.
///
/// The card compiles and is wired to real state, but "it builds" says nothing
/// about whether its text is clipped, its buttons overlap, or its warning is
/// legible on a dark background. This is the difference between a card that
/// exists and one a person can read.
///
/// It is reached by a launch argument, not a separate SwiftPM target: the card
/// is module-internal and `PendingDesktopApproval` wraps `APIModels.Mission`,
/// so a preview inside the module needs no visibility changes and no second
/// build graph to keep in step.
///
///     Vantage --render-approval-cards /tmp/vantage-cards
enum ApprovalCardPreview {

    static func render(to directory: String) -> Int32 {
        let root = URL(fileURLWithPath: directory, isDirectory: true)
        try? FileManager.default.createDirectory(
            atPath: root.path, withIntermediateDirectories: true)

        var written = 0
        for (name, approval) in cases() {
            for (label, built) in [("light", NSAppearance(named: .aqua)),
                                   ("dark", NSAppearance(named: .darkAqua))] {
                guard let appearance = built else { continue }
                guard let image = snapshot(approval, appearance: appearance) else {
                    fputs("could not render \(name) \(label)\n", stderr)
                    continue
                }
                let url = root.appendingPathComponent("\(name)-\(label).png")
                guard let data = image.representation(
                    using: .png, properties: [:]) else {
                    fputs("could not encode \(url.lastPathComponent)\n", stderr)
                    continue
                }
                do {
                    try data.write(to: url)
                    written += 1
                    print("\(url.path)  \(data.count) bytes")
                } catch {
                    fputs("could not write \(url.path): \(error)\n", stderr)
                }
            }
        }
        return written > 0 ? 0 : 1
    }

    // MARK: - The cases

    /// The case that matters most: an ungated step already ran, and a destructive
    /// one is waiting. A plan can move the pointer without approval and then
    /// park on the click, so "nothing has run" would be a lie.
    private static func mixedPlan() -> PendingDesktopApproval {
        approval(
            missionID: "MIS-3f8a1c",
            summary: "Run desktop_click on this Mac.",
            confirmation: "CONF-8c31f0a2b9",
            steps: [
                ("S1", "Move the pointer to (412, 336)", "done",
                 "desktop_move_pointer", "the real pointer is at (412, 336)"),
                ("S2", "Click at (412, 336)", "pending",
                 "desktop_click", "awaiting approval: level 3 destructive"),
                ("S3", "Type Hello from Vantage", "pending",
                 "desktop_type_text", ""),
            ])
    }

    /// No confirmation id: the card must suppress Approve and say why, rather
    /// than offering a button that would fail when pressed.
    private static func noConfirmation() -> PendingDesktopApproval {
        approval(
            missionID: "MIS-1d77b4",
            summary: "Run desktop_type_text on this Mac.",
            confirmation: nil,
            steps: [
                ("S1", "Type a note", "pending", "desktop_type_text", ""),
            ])
    }

    /// Nothing is parked: both buttons must be suppressed.
    private static func nothingParked() -> PendingDesktopApproval {
        approval(
            missionID: "MIS-9a02ce",
            summary: "Run desktop_scroll on this Mac.",
            confirmation: "CONF-11aa22bb",
            steps: [
                ("S1", "Scroll 3 lines at (200, 200)", "done",
                 "desktop_scroll", "scrolled 3 lines"),
            ])
    }

    private static func cases() -> [(String, PendingDesktopApproval)] {
        [("case1-already-ran", mixedPlan()),
         ("case2-no-confirmation", noConfirmation()),
         ("case3-nothing-parked", nothingParked())]
    }

    // MARK: - Construction

    private static func approval(missionID: String, summary: String,
                                 confirmation: String?,
                                 steps: [(String, String, String, String, String)])
        -> PendingDesktopApproval {
        let built = steps.map { step -> PendingDesktopApproval.Step in
            .init(id: step.0, title: step.1, status: step.2,
                  tool: step.3, detail: step.4,
                  paramsSummary: paramsSummary(for: step.3))
        }
        return PendingDesktopApproval(
            mission: mission(id: missionID, steps: steps),
            missionId: missionID,
            confirmationId: confirmation,
            steps: built,
            summary: summary)
    }

    /// A one-line rendering of the step's parameters, the way the service builds
    /// it -- the card must show what will happen, not only that something will.
    private static func paramsSummary(for tool: String) -> String {
        switch tool {
        case "desktop_click", "desktop_move_pointer":
            return "x: 412, y: 336"
        case "desktop_type_text":
            return "text: Hello from Vantage"
        case "desktop_scroll":
            return "x: 200, y: 200, lines: -3"
        default:
            return ""
        }
    }

    /// `Mission` is Codable with no memberwise initialiser, so a fixture is
    /// cheaper and less brittle to decode than to hand-build.
    private static func mission(id: String,
                                steps: [(String, String, String, String, String)])
        -> APIModels.Mission {
        let stepJSON = steps.map { step -> String in
            let detail = step.4.isEmpty ? "" : ", \"detail\": \"\(step.4)\""
            return """
            {"step_id":"\(step.0)","title":"\(step.1)","tool":"\(step.3)",\
            "params":{},"status":"\(step.2)"\(detail),\
            "satisfies":"","idempotency_key":"","depends_on":[]}
            """
        }.joined(separator: ",")
        let json = """
        {"mission_id":"\(id)","request":"open TextEdit and type","state":\
        "awaiting_approval","verdict":"unknown","steps":[\(stepJSON)],\
        "plan_revision":0}
        """
        let decoded = try? JSONDecoder().decode(
            APIModels.Mission.self, from: Data(json.utf8))
        return decoded ?? emptyMission(id: id)
    }

    private static func emptyMission(id: String) -> APIModels.Mission {
        let json = """
        {"mission_id":"\(id)","request":"","state":"awaiting_approval",\
        "verdict":"unknown","steps":[],"plan_revision":0}
        """
        // The fixture above is the same shape, so this is only a last resort.
        return (try? JSONDecoder().decode(
            APIModels.Mission.self, from: Data(json.utf8)))!
    }

    // MARK: - Rendering

    private static func snapshot(_ approval: PendingDesktopApproval,
                                 appearance: NSAppearance) -> NSBitmapImageRep? {
        let card = DesktopPlanApprovalCard(
            approval: approval,
            isDeciding: false,
            onApprove: {},
            onDeny: {})

        let hosting = NSHostingView(rootView: card)
        hosting.frame = NSRect(x: 0, y: 0, width: 640, height: 10)
        hosting.appearance = appearance

        // Size to the content rather than to a guessed height: a clipped card
        // would be the exact defect this exists to find.
        let fitting = hosting.fittingSize
        let size = NSSize(width: 640, height: max(fitting.height, 80))
        hosting.frame = NSRect(origin: .zero, size: size)

        guard let bitmap = hosting.bitmapImageRepForCachingDisplay(
            in: hosting.bounds) else { return nil }
        hosting.cacheDisplay(in: hosting.bounds, to: bitmap)

        return bitmap
    }
}