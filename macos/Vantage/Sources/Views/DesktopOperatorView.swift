import Foundation
import SwiftUI

/// Desktop Operator: five panels over the real `/desktop` routes.
///
/// The screen exists to make the distance between "asked" and "did" visible.
/// Every button calls a route and reports the backend's own `status` and
/// `reason` into one Result panel; nothing here upgrades a refusal into a
/// success, and nothing runs without a capability the backend reported as
/// granted.
///
/// Two rules are load-bearing and are why the code looks the way it does:
///
/// 1. **No coordinate default.** A malformed x/y is refused here, visibly, and
///    nothing is sent. `Double(...) ?? 0` would move the real pointer to the
///    screen corner, which is the exact bug this project already fixed once.
/// 2. **A window pin is a claim the backend re-checks.** Selecting a window
///    stores its `window_id` and sends it as `expect_window_id`; the executor
///    refuses the click if that window has moved, closed or changed owner.
///    Cursor moves cannot be pinned and the screen says so.
struct DesktopOperatorView: View {

    private let api = APIClient.shared

    @State private var posture: APIModels.DesktopPosture?
    @State private var postureError: String?
    @State private var loadingPosture = false

    @State private var application = ""
    @State private var windows: [OperatorWindow] = []
    @State private var selectedWindowID: Int?
    @State private var windowsRead = false

    @State private var xText = ""
    @State private var yText = ""
    @State private var elementLabel = ""

    @State private var inspection: Inspection?
    @State private var probeLegs: [OperatorProbeLeg] = []

    @State private var requestText = ""
    @State private var plan: OperatorPlanReply?
    @State private var planRequested = false

    /// What is running right now, by display label. One action at a time keeps
    /// the Result panel unambiguous about which request it describes.
    @State private var busy: String?
    @State private var outcome: Outcome?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Desktop Operator",
                    subtitle: "Windows, coordinates, actions and plans — every control calls the backend's real /desktop route."
                ) {
                    Button(loadingPosture ? "Reading…" : "Refresh capabilities") {
                        Task { await loadPosture() }
                    }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(loadingPosture || busy != nil)
                }

                if let postureError {
                    ErrorBanner(
                        message: "The desktop capability could not be read: \(postureError)",
                        retry: { Task { await loadPosture() } }
                    )
                }

                applicationsPanel
                targetPanel
                actionsPanel
                plannerPanel
                resultPanel
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await loadPosture() }
    }

    // MARK: - Applications

    private var applicationsPanel: some View {
        card("Applications") {
            HStack(spacing: V.Space.s) {
                TextField("Application name — blank lists every on-screen window",
                          text: $application)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 340)
                Button(busy == OperatorLabels.listWindows ? "Listing…" : "List windows") {
                    Task { await listWindows() }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil || screenCaptureReason != nil)
                Spacer(minLength: 0)
            }

            if let reason = screenCaptureReason {
                warning(reason)
            }

            if let pinned = selectedWindow {
                // Stated here, next to the selection, because it is the one
                // fact that decides whether a click is safe to press.
                Notice(
                    kind: .info,
                    message: "Clicks are pinned to window \(pinned.windowID) "
                        + "(\((pinned.owner ?? "unknown owner"))\(pinned.titleSuffix)). "
                        + "Clicks will be refused if this window moves: the backend re-reads "
                        + "the window before posting and sends nothing when it cannot match it. "
                        + "Cursor moves are not pinned."
                )
            }

            if windowsRead && windows.isEmpty {
                Text("The backend returned no windows for that filter. Its own reason is in the Result panel below.")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !windows.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(windows) { window in
                            windowRow(window)
                        }
                    }
                    .padding(.vertical, V.Space.xxs)
                }
                .frame(maxHeight: 190)
                Text("Click a row to pin clicks to that window; click it again to unpin.")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textTertiary)
            }
        }
    }
    private func windowRow(_ window: OperatorWindow) -> some View {
        let selected = window.windowID == selectedWindowID
        return Button {
            selectedWindowID = selected ? nil : window.windowID
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 11))
                    .foregroundStyle(selected ? V.accent : V.textTertiary)
                VStack(alignment: .leading, spacing: 1) {
                    Text("\(window.owner ?? "unknown") — \(window.title ?? "untitled")")
                        .font(V.Font.label)
                        .foregroundStyle(V.text)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(window.geometryText)
                        .font(V.Font.mono)
                        .foregroundStyle(V.textTertiary)
                        .lineLimit(1)
                }
                Spacer(minLength: V.Space.s)
            }
            .padding(.horizontal, V.Space.s)
            .padding(.vertical, 5)
            .background(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .fill(selected ? V.accentSoft : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(window.owner ?? "unknown") \(window.title ?? "untitled"), window \(window.windowID)")
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }

    private var selectedWindow: OperatorWindow? {
        guard let selectedWindowID else { return nil }
        return windows.first { $0.windowID == selectedWindowID }
    }

    // MARK: - Target

    private var targetPanel: some View {
        card("Target") {
            HStack(spacing: V.Space.s) {
                coordinateField("x", text: $xText)
                coordinateField("y", text: $yText)
                Button(busy == OperatorLabels.inspect ? "Inspecting…" : "Inspect UI") {
                    Task { await inspectUI() }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil || readUIReason != nil)
                Button(busy == OperatorLabels.focus ? "Focusing…" : "Focus App") {
                    Task { await focusApplication() }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil || application.trimmingCharacters(in: .whitespaces).isEmpty)
                Spacer(minLength: 0)
            }

            if let reason = readUIReason {
                warning(reason)
            }
            if application.trimmingCharacters(in: .whitespaces).isEmpty {
                Text("Focus App needs an application name in the Applications panel.")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textTertiary)
            }
            if let pinned = selectedWindow {
                Text("Clicks are pinned to window \(pinned.windowID) — they are refused if it moves.")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textSecondary)
            }

            HStack(spacing: V.Space.s) {
                TextField("Element label — filters the inspected tree", text: $elementLabel)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 340)
                Spacer(minLength: 0)
            }
            // The inspect route accepts an application and a depth, not a
            // label, so the label is described as what it really is here: a
            // filter over the tree the backend returned, applied on this side.
            Text("The label is not sent: /desktop/inspect takes the application and depth only, so typing here filters the delivered tree.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)

            if let inspection {
                Divider().overlay(V.divider)
                Text("\(inspection.elementCount.map(String.init) ?? "?") element(s) read; showing \(inspectedRows.count).")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textSecondary)
                if inspectedRows.isEmpty {
                    Text(elementLabel.trimmingCharacters(in: .whitespaces).isEmpty
                         ? "The tree carried no readable nodes."
                         : "No node's role, title or value contains “\(elementLabel)”.")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 1) {
                            ForEach(inspectedRows) { row in
                                Text(row.line)
                                    .font(V.Font.mono)
                                    .foregroundStyle(row.hasTitle ? V.text : V.textTertiary)
                                    .lineLimit(1)
                                    .truncationMode(.tail)
                            }
                        }
                    }
                    .frame(maxHeight: 180)
                }
            }
        }
    }

    private func coordinateField(_ label: String, text: Binding<String>) -> some View {
        HStack(spacing: 4) {
            Text(label).font(V.Font.caption).foregroundStyle(V.textTertiary)
            TextField(label, text: text)
                .textFieldStyle(.roundedBorder)
                .frame(width: 84)
        }
    }

    // MARK: - Actions

    private var actionsPanel: some View {
        card("Actions") {
            HStack(spacing: V.Space.s) {
                Button(busy == OperatorLabels.move ? "Moving…" : "Move Cursor") {
                    Task { await moveCursor() }
                }
                .buttonStyle(VPrimaryButtonStyle())
                .disabled(busy != nil || moveReason != nil)
                Button(busy == OperatorLabels.click ? "Clicking…" : "Click") {
                    Task { await click(button: "left", count: 1, label: OperatorLabels.click) }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil || clickReason != nil)
                Button(busy == OperatorLabels.doubleClick ? "Clicking…" : "Double-click") {
                    Task { await click(button: "left", count: 2, label: OperatorLabels.doubleClick) }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil || clickReason != nil)
                Button(busy == OperatorLabels.rightClick ? "Clicking…" : "Right-click") {
                    Task { await click(button: "right", count: 1, label: OperatorLabels.rightClick) }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil || clickReason != nil)
                Spacer(minLength: 0)
            }
            if let reason = moveReason { warning(reason) }
            if let reason = clickReason { warning(reason) }

            HStack(spacing: V.Space.s) {
                Button(busy == OperatorLabels.probe ? "Testing…" : "Test Pointer") {
                    Task { await testPointer() }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil || moveReason != nil)
                Button(busy == OperatorLabels.release ? "Releasing…" : "Release Held Input") {
                    Task { await releaseInput() }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil || clickReason != nil)
                Spacer(minLength: 0)
            }
            Text("Test Pointer moves the pointer through a bounded square and reads its position back after each leg; it posts no click.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
            Text("Release Held Input is recovery for a drag that was cancelled or left a button down.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)

            if !probeLegs.isEmpty {
                Divider().overlay(V.divider)
                Text("POINTER TEST EVIDENCE")
                    .font(V.Font.caption.weight(.semibold))
                    .foregroundStyle(V.textTertiary)
                // Each leg is what was asked for and what the pointer read
                // back. Showing both is the difference between evidence and a
                // sentence claiming the pointer went there.
                ForEach(Array(probeLegs.enumerated()), id: \.offset) { index, leg in
                    Text("leg \(index + 1) — asked \(OperatorProbeLeg.pointText(leg.target)) → pointer read \(OperatorProbeLeg.pointText(leg.observed))")
                        .font(V.Font.mono)
                        .foregroundStyle(V.textSecondary)
                }
            }
        }
    }

    // MARK: - Planner

    private var plannerPanel: some View {
        card("Planner") {
            HStack(spacing: V.Space.s) {
                TextField("e.g. click 600 400 and type \"done\"", text: $requestText)
                    .textFieldStyle(.roundedBorder)
                Button(busy == OperatorLabels.plan ? "Planning…" : "Preview Plan") {
                    Task { await previewPlan() }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(busy != nil
                          || requestText.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            Text("Preview only: this asks the planner what it would do. Pressing it executes nothing.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)

            if planRequested, let plan {
                if let clarification = plan.clarification,
                   !clarification.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    // The planner returns zero steps rather than guessing, so
                    // this branch must say that in as many words.
                    Notice(
                        kind: .warning,
                        message: "Nothing was planned. The planner asked a question instead of guessing at a target."
                    )
                    Text(clarification)
                        .font(V.Font.body)
                        .foregroundStyle(V.text)
                        .fixedSize(horizontal: false, vertical: true)
                } else if (plan.steps ?? []).isEmpty {
                    Text("The planner returned no steps and no question.")
                        .font(V.Font.caption)
                        .foregroundStyle(V.warning)
                } else {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        ForEach(Array((plan.steps ?? []).enumerated()), id: \.offset) { index, step in
                            planStepRow(index: index, step: step)
                        }
                    }
                }
                if let notes = plan.notes, !notes.isEmpty {
                    Text(notes.joined(separator: " "))
                        .font(V.Font.caption)
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func planStepRow(index: Int, step: OperatorPlanStep) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Text("\(index + 1).")
                .font(V.Font.mono)
                .foregroundStyle(V.textTertiary)
            VStack(alignment: .leading, spacing: 2) {
                Text(step.description.isEmpty ? step.tool : step.description)
                    .font(V.Font.body)
                    .foregroundStyle(V.text)
                    .fixedSize(horizontal: false, vertical: true)
                Text(step.tool)
                    .font(V.Font.mono)
                    .foregroundStyle(V.textSecondary)
                if let params = step.params, !params.isEmpty {
                    Text(step.paramsText(params))
                        .font(V.Font.mono)
                        .foregroundStyle(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    // MARK: - Result

    private var resultPanel: some View {
        card("Result", background: V.surfaceAlt) {
            if let outcome {
                HStack(spacing: V.Space.s) {
                    Image(systemName: statusSymbol(outcome.status))
                        .font(.system(size: 12))
                        .foregroundStyle(statusColour(outcome.status))
                    Text("\(outcome.operation): \(outcome.status)")
                        .font(V.Font.label)
                        .foregroundStyle(statusColour(outcome.status))
                    Spacer(minLength: 0)
                }
                Text("Target: \(outcome.target)")
                    .font(V.Font.mono)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                // The backend's sentence, unedited. A refusal reads as a
                // refusal here because it is the executor's own wording.
                Text(outcome.reason.isEmpty ? "The backend returned no reason." : outcome.reason)
                    .font(V.Font.body)
                    .foregroundStyle(V.text)
                    .fixedSize(horizontal: false, vertical: true)
                if let footnote = outcome.footnote {
                    Text(footnote)
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !outcome.sent {
                    Text("Nothing was sent to the backend — this refusal was raised on this side.")
                        .font(V.Font.caption)
                        .foregroundStyle(V.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text("No action has run yet. Every control above reports here with the backend's own status and reason.")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - Capabilities

    /// The four capabilities this screen gates on, named by what they permit.
    private enum CapabilityGate {
        case move, click, readUI, screenCapture

        var action: String {
            switch self {
            case .move: return "Moving the pointer"
            case .click: return "Clicking and releasing input"
            case .readUI: return "Inspecting the interface"
            case .screenCapture: return "Listing windows"
            }
        }

        var permission: String {
            switch self {
            case .move: return "cursor movement"
            case .click: return "posting input events"
            case .readUI: return "Accessibility access to read the interface"
            case .screenCapture: return "Screen Recording, which the window list needs"
            }
        }
    }

    /// `nil` means the backend did not say, which is *not* a yes.
    private func granted(_ gate: CapabilityGate) -> Bool? {
        guard let posture else { return nil }
        if let map = posture.capabilities {
            switch gate {
            case .move: return map.move_cursor
            case .click: return map.click
            case .readUI: return map.read_ui
            case .screenCapture: return map.screen_capture
            }
        }
        switch gate {
        case .move: return posture.can_move_cursor
        case .click: return posture.can_click
        case .readUI: return posture.can_read_ui
        case .screenCapture: return posture.screen_capture
        }
    }

    /// The sentence shown beside a disabled control. Disabled controls are
    /// kept visible with this reason attached instead of being hidden.
    private func disabledReason(_ gate: CapabilityGate) -> String? {
        switch granted(gate) {
        case .some(true):
            return nil
        case .some(false):
            return "\(gate.action) is disabled: the backend reports \(gate.permission) as not granted for the desktop executor. "
                + "Grant it in System Settings › Privacy & Security, then press Refresh capabilities."
        case .none:
            return posture == nil
                ? "\(gate.action) is disabled until the desktop capability has been read: nothing is assumed to be granted."
                : "\(gate.action) is disabled: the backend did not report \(gate.permission), and an unreported capability is not a granted one."
        }
    }

    private var moveReason: String? { disabledReason(.move) }
    private var clickReason: String? { disabledReason(.click) }
    private var readUIReason: String? { disabledReason(.readUI) }
    private var screenCaptureReason: String? { disabledReason(.screenCapture) }

    // MARK: - Views

    private func card<Content: View>(_ title: String,
                                     background: Color = V.surface,
                                     @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text(title.uppercased())
                .font(V.Font.caption.weight(.semibold))
                .tracking(0.6)
                .foregroundStyle(V.textTertiary)
            content()
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(background)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func warning(_ text: String) -> some View {
        Text(text)
            .font(V.Font.caption)
            .foregroundStyle(V.warning)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func statusSymbol(_ status: String) -> String {
        switch status {
        case "verified": return "checkmark.circle.fill"
        case "performed_unverified": return "questionmark.circle.fill"
        case "permission_required", "blocked", "unknown": return "lock.fill"
        case "refused_locally": return "exclamationmark.triangle.fill"
        default: return "xmark.circle.fill"
        }
    }

    private func statusColour(_ status: String) -> Color {
        switch status {
        case "verified": return V.success
        // Sent but unobserved is amber, never green.
        case "performed_unverified", "permission_required", "blocked",
             "target_not_found", "target_ambiguous", "target_stale",
             "timed_out", "cancelled", "unknown":
            return V.warning
        default: return V.danger
        }
    }

    // MARK: - Data

    @MainActor
    private func loadPosture() async {
        loadingPosture = true
        defer { loadingPosture = false }
        do {
            posture = try await api.get("/desktop", as: APIModels.DesktopPosture.self)
            postureError = nil
        } catch {
            posture = nil
            postureError = error.localizedDescription
        }
    }

    @MainActor
    private func listWindows() async {
        begin(OperatorLabels.listWindows)
        defer { busy = nil }
        let app = application.trimmingCharacters(in: .whitespaces)
        let target = app.isEmpty ? "all on-screen windows" : "application “\(app)”"
        let query = queryString([URLQueryItem(name: "application", value: app)])
        do {
            let reply: OperatorWindowsReply = try await api.get(
                "/desktop/windows?\(query)", as: OperatorWindowsReply.self)
            windows = reply.windows ?? []
            windowsRead = true
            // A pin must not outlive the window it named.
            if let selectedWindowID,
               !windows.contains(where: { $0.windowID == selectedWindowID }) {
                self.selectedWindowID = nil
            }
            record(OperatorLabels.listWindows, target: target,
                   status: reply.status, reason: reply.reason, sent: true)
        } catch {
            windows = []
            windowsRead = true
            recordFailure(OperatorLabels.listWindows, target: target, error: error)
        }
    }

    @MainActor
    private func moveCursor() async {
        guard let point = typedPoint(operation: OperatorLabels.move) else { return }
        begin(OperatorLabels.move)
        defer { busy = nil }
        let target = pointText(point)
        do {
            let reply: OperatorActionResult = try await api.post(
                "/desktop/move",
                body: MoveRequest(x: point.x, y: point.y),
                as: OperatorActionResult.self)
            record(OperatorLabels.move, target: target, status: reply.status,
                   reason: reply.reason, sent: true)
        } catch {
            recordFailure(OperatorLabels.move, target: target, error: error)
        }
    }

    @MainActor
    private func click(button: String, count: Int, label: String) async {
        guard let point = typedPoint(operation: label) else { return }
        begin(label)
        defer { busy = nil }
        let pin = selectedWindowID
        let target = pointText(point) + (pin.map { " · window \($0)" } ?? "")
        do {
            let reply: OperatorActionResult = try await api.post(
                "/desktop/click",
                body: ClickRequest(x: point.x, y: point.y, button: button,
                                   count: count, expect_window_id: pin),
                as: OperatorActionResult.self)
            record(label, target: target, status: reply.status,
                   reason: reply.reason, sent: true)
        } catch {
            recordFailure(label, target: target, error: error)
        }
    }

    @MainActor
    private func testPointer() async {
        begin(OperatorLabels.probe)
        defer { busy = nil }
        do {
            let reply: OperatorProbeReply = try await api.post(
                "/desktop/probe", body: Optional<String>.none,
                as: OperatorProbeReply.self)
            probeLegs = reply.legs ?? reply.legs_checked ?? []
            let target = probeLegs.isEmpty ? "bounded square from the current pointer" : "\(probeLegs.count) leg(s)"
            record(OperatorLabels.probe, target: target, status: reply.status,
                   reason: reply.reason, sent: true)
        } catch {
            probeLegs = []
            recordFailure(OperatorLabels.probe,
                          target: "bounded square from the current pointer", error: error)
        }
    }

    @MainActor
    private func releaseInput() async {
        begin(OperatorLabels.release)
        defer { busy = nil }
        let target = "held synthetic input"
        do {
            let reply: OperatorActionResult = try await api.post(
                "/desktop/release-input", body: Optional<String>.none,
                as: OperatorActionResult.self)
            record(OperatorLabels.release, target: target, status: reply.status,
                   reason: reply.reason, sent: true)
        } catch {
            recordFailure(OperatorLabels.release, target: target, error: error)
        }
    }

    @MainActor
    private func inspectUI() async {
        begin(OperatorLabels.inspect)
        defer { busy = nil }
        let app = application.trimmingCharacters(in: .whitespaces)
        let target = (app.isEmpty ? "front application" : app) + ", depth 6"
        let query = queryString([
            URLQueryItem(name: "application", value: app),
            URLQueryItem(name: "depth", value: "6"),
        ])
        do {
            let reply: OperatorInspectReply = try await api.get(
                "/desktop/inspect?\(query)", as: OperatorInspectReply.self)
            // A failed read clears the old tree rather than leaving a stale one
            // on screen under a new status.
            inspection = reply.tree.map {
                Inspection(tree: $0, elementCount: reply.element_count)
            }
            record(OperatorLabels.inspect, target: target, status: reply.status,
                   reason: reply.reason, sent: true)
        } catch {
            inspection = nil
            recordFailure(OperatorLabels.inspect, target: target, error: error)
        }
    }

    @MainActor
    private func focusApplication() async {
        begin(OperatorLabels.focus)
        defer { busy = nil }
        let app = application.trimmingCharacters(in: .whitespaces)
        guard !app.isEmpty else {
            record(OperatorLabels.focus, target: "no application named",
                   status: "refused_locally",
                   reason: "Type an application name in the Applications panel. Nothing was sent, because focusing the front application is not the same request as focusing the one you meant.",
                   sent: false)
            return
        }
        do {
            let reply: OperatorActionResult = try await api.post(
                "/desktop/focus", body: FocusRequest(application: app),
                as: OperatorActionResult.self)
            var reason = reply.reason
            if let front = reply.frontmost_after, !front.isEmpty {
                reason += " (frontmost after the request: \(front))"
            }
            record(OperatorLabels.focus, target: "application “\(app)”",
                   status: reply.status, reason: reason, sent: true)
        } catch {
            recordFailure(OperatorLabels.focus, target: "application “\(app)”", error: error)
        }
    }

    @MainActor
    private func previewPlan() async {
        begin(OperatorLabels.plan)
        defer { busy = nil }
        let request = requestText.trimmingCharacters(in: .whitespaces)
        guard !request.isEmpty else {
            record(OperatorLabels.plan, target: "empty request", status: "refused_locally",
                   reason: "Type an instruction first. The planner is never called with nothing to plan.",
                   sent: false)
            return
        }
        let query = queryString([URLQueryItem(name: "request", value: request)])
        do {
            let reply: OperatorPlanReply = try await api.get(
                "/desktop/plan?\(query)", as: OperatorPlanReply.self)
            plan = reply
            planRequested = true

            // The plan route has no `reason` field; the planner's own notes and
            // clarification are the backend's words about the request, so they
            // are what the Result panel carries verbatim.
            let clarification = reply.clarification?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let reason: String
            if !clarification.isEmpty {
                reason = clarification
            } else if let notes = reply.notes, !notes.isEmpty {
                reason = notes.joined(separator: " ")
            } else {
                reason = "The planner returned no notes and no clarification."
            }
            record(OperatorLabels.plan, target: "“\(request)”", status: reply.status,
                   reason: reason, sent: true,
                   footnote: "Preview only — planning does not execute anything; no desktop action was sent.")
        } catch {
            plan = nil
            planRequested = true
            recordFailure(OperatorLabels.plan, target: "“\(request)”", error: error)
        }
    }

    /// The typed coordinate, or `nil` after recording why it was refused.
    ///
    /// There is deliberately no default: `Double(xText) ?? 0` used to send a
    /// typo to (0, 0), the top-left corner of the screen, and move the real
    /// pointer there without a word.
    @MainActor
    private func typedPoint(operation: String) -> (x: Double, y: Double)? {
        let rawX = xText.trimmingCharacters(in: .whitespaces)
        let rawY = yText.trimmingCharacters(in: .whitespaces)
        let target = "x “\(rawX)”, y “\(rawY)”"
        guard let x = Double(rawX), let y = Double(rawY) else {
            record(operation, target: target, status: "refused_locally",
                   reason: "Both coordinate fields must be numbers, one in each field. Nothing was sent: a malformed value must not become a guessed pointer position.",
                   sent: false)
            return nil
        }
        // `Double("inf")` and `Double("nan")` parse; neither is a screen point.
        guard x.isFinite, y.isFinite else {
            record(operation, target: target, status: "refused_locally",
                   reason: "Coordinates must be finite numbers. Nothing was sent.",
                   sent: false)
            return nil
        }
        return (x, y)
    }

    private func pointText(_ point: (x: Double, y: Double)) -> String {
        "(\(OperatorProbeLeg.number(point.x)), \(OperatorProbeLeg.number(point.y)))"
    }

    @MainActor
    private func begin(_ operation: String) {
        busy = operation
        // Pointer evidence describes one probe; another action must not leave
        // it on screen looking like that action's result.
        if operation != OperatorLabels.probe { probeLegs = [] }
    }

    @MainActor
    private func record(_ operation: String, target: String, status: String,
                        reason: String, sent: Bool, footnote: String? = nil) {
        outcome = Outcome(operation: operation, target: target, status: status,
                          reason: reason, sent: sent, footnote: footnote)
    }

    @MainActor
    private func recordFailure(_ operation: String, target: String, error: Error) {
        let described = describe(error)
        record(operation, target: target, status: described.status,
               reason: described.reason, sent: true)
    }

    /// A thrown error as a status and the backend's own sentence where one exists.
    private func describe(_ error: Error) -> (status: String, reason: String) {
        guard let apiError = error as? APIError else {
            return ("error", error.localizedDescription)
        }
        switch apiError {
        case .httpError(let code, let detail):
            // `detail` is the backend's refusal sentence (a 409 takeover, a 422
            // rejected point); it is shown unchanged.
            let sentence = detail.trimmingCharacters(in: .whitespacesAndNewlines)
            return ("HTTP \(code)", sentence.isEmpty ? apiError.localizedDescription : sentence)
        case .unauthorized:
            return ("unauthorized", apiError.localizedDescription)
        case .rateLimited:
            return ("rate_limited", apiError.localizedDescription)
        case .invalidURL(let url):
            return ("invalid_url", "Invalid URL: \(url)")
        case .transport(let message):
            return ("unreachable", "Cannot reach backend: \(message)")
        case .decodeError(let message):
            return ("unreadable_response", "Unexpected response format: \(message)")
        }
    }

    /// Query items encoded without disturbing the API client's path handling.
    private func queryString(_ items: [URLQueryItem]) -> String {
        var components = URLComponents()
        components.queryItems = items
        return components.percentEncodedQuery ?? ""
    }

    // MARK: - Inspected tree

    private var inspectedRows: [OperatorFlatNode] {
        guard let inspection else { return [] }
        var rows: [OperatorFlatNode] = []
        flatten(inspection.tree, depth: 0, into: &rows)
        let needle = elementLabel.trimmingCharacters(in: .whitespaces).lowercased()
        let matched: [OperatorFlatNode]
        if needle.isEmpty {
            matched = rows
        } else {
            matched = rows.filter {
                $0.role.lowercased().contains(needle)
                    || $0.title.lowercased().contains(needle)
                    || $0.value.lowercased().contains(needle)
            }
        }
        return Array(matched.prefix(40))
    }

    /// Flattens the Accessibility tree, bounded so a pathological tree cannot
    /// stall the view. The bound is on reading, not on what the backend sent.
    private func flatten(_ node: OperatorNode, depth: Int, into rows: inout [OperatorFlatNode]) {
        guard rows.count < 400 else { return }
        rows.append(OperatorFlatNode(
            depth: min(depth, 8),
            role: node.role ?? "?",
            title: node.title ?? node.label ?? "",
            value: node.value?.displayText ?? ""
        ))
        for child in node.children ?? [] {
            flatten(child, depth: depth + 1, into: &rows)
        }
    }
}

// MARK: - Labels

/// One place for the operation names, so a `busy` comparison and the text that
/// appears in the Result panel can never drift apart.
private enum OperatorLabels {
    static let listWindows = "List windows"
    static let move = "Move cursor"
    static let click = "Click"
    static let doubleClick = "Double-click"
    static let rightClick = "Right-click"
    static let probe = "Test pointer"
    static let release = "Release held input"
    static let inspect = "Inspect UI"
    static let focus = "Focus app"
    static let plan = "Preview plan"
}

// MARK: - Request bodies

private struct MoveRequest: Encodable {
    let x: Double
    let y: Double
}

private struct ClickRequest: Encodable {
    let x: Double
    let y: Double
    let button: String
    let count: Int
    /// Nil is omitted by the synthesized encoder, so an unpinned click sends
    /// no expectation at all — which is the old, unpinned behaviour.
    let expect_window_id: Int?
}

private struct FocusRequest: Encodable {
    let application: String
}

// MARK: - Reply models

private struct OperatorWindowsReply: Decodable {
    let status: String
    let reason: String
    let windows: [OperatorWindow]?
}

/// One on-screen window as the executor reports it.
private struct OperatorWindow: Decodable, Identifiable {
    let windowID: Int
    let owner: String?
    let title: String?
    let x: Double?
    let y: Double?
    let width: Double?
    let height: Double?

    var id: Int { windowID }

    enum CodingKeys: String, CodingKey {
        case windowID = "window_id"
        case owner, title, x, y, width, height
    }

    var titleSuffix: String { (title?.isEmpty ?? true) ? "" : " — \(title ?? "")" }

    /// Size and position in one line; a field the executor omitted is named
    /// rather than printed as zero.
    var geometryText: String {
        let size = width.flatMap { w in height.map { h in "\(OperatorProbeLeg.number(w))×\(OperatorProbeLeg.number(h))" } }
        let origin = x.flatMap { px in y.map { py in "(\(OperatorProbeLeg.number(px)), \(OperatorProbeLeg.number(py)))" } }
        var parts = ["id \(windowID)"]
        parts.append(size ?? "size not reported")
        parts.append(origin.map { "at \($0)" } ?? "position not reported")
        return parts.joined(separator: " · ")
    }
}

private struct OperatorActionResult: Decodable {
    let status: String
    let reason: String
    let cursor_before: APIModels.DesktopPoint?
    let cursor_after: APIModels.DesktopPoint?
    let frontmost_after: String?
}

private struct OperatorProbeReply: Decodable {
    let status: String
    let reason: String
    /// Present when every leg verified; the partial-failure reply is
    /// `legs_checked`. Both are decoded so evidence read before a failure is
    /// still shown.
    let legs: [OperatorProbeLeg]?
    let legs_checked: [OperatorProbeLeg]?
}

private struct OperatorProbeLeg: Decodable {
    let target: [Double?]
    let observed: [Double?]

    static func pointText(_ values: [Double?]) -> String {
        guard values.count >= 2, let x = values[0], let y = values[1] else { return "?" }
        return "(\(number(x)), \(number(y)))"
    }

    /// `%g` keeps an integral readback as "640" and a fractional one as
    /// "640.5", without inventing precision the pointer reading did not have.
    static func number(_ value: Double) -> String {
        String(format: "%g", value)
    }
}

private struct OperatorInspectReply: Decodable {
    let status: String
    let reason: String
    let tree: OperatorNode?
    let element_count: Int?
}

private struct OperatorNode: Decodable {
    let role: String?
    let title: String?
    let label: String?
    let value: AnyCodable?
    let children: [OperatorNode]?
}

private struct OperatorPlanReply: Decodable {
    let status: String
    let steps: [OperatorPlanStep]?
    let clarification: String?
    let notes: [String]?
}

private struct OperatorPlanStep: Decodable {
    let tool: String
    let params: [String: AnyCodable]?
    let description: String

    /// The parameters as one line, alphabetised so two identical plans render
    /// identically.
    func paramsText(_ params: [String: AnyCodable]) -> String {
        params.map { "\($0.key)=\($0.value.displayText)" }.sorted().joined(separator: " · ")
    }
}

// MARK: - View-local types

private struct Inspection {
    let tree: OperatorNode
    let elementCount: Int?
}

private struct OperatorFlatNode: Identifiable {
    let id = UUID()
    let depth: Int
    let role: String
    let title: String
    let value: String

    var hasTitle: Bool { !title.isEmpty || !value.isEmpty }

    var line: String {
        let indent = String(repeating: "  ", count: depth)
        var parts = [role]
        if !title.isEmpty { parts.append("“\(title)”") }
        if !value.isEmpty { parts.append("= \(value)") }
        return indent + parts.joined(separator: " ")
    }
}

/// One action's report. The Result panel is the only place these render, so a
/// refusal can never be rewritten into a success somewhere else.
private struct Outcome {
    let operation: String
    let target: String
    let status: String
    /// The backend's sentence, verbatim.
    let reason: String
    /// False for a refusal raised here, which the panel says out loud.
    let sent: Bool
    var footnote: String? = nil
}