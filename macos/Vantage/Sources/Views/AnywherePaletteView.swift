import SwiftUI
import AppKit

/// The Vantage Anywhere palette.
///
/// Compact by design: one field, one short list, one result line. Everything it
/// can do goes through `AnywhereCommand` -> `MacControlCoordinator`, which is the
/// same validated path Control Mac and Command Center use. The palette cannot
/// execute anything the app would refuse elsewhere, and it says when a step will
/// need approval instead of silently doing it.
struct AnywherePaletteView: View {

    @ObservedObject var controller: AnywhereController
    @ObservedObject private var control = MacControlCoordinator.shared
    @ObservedObject private var flows = FlowStore.shared

    @State private var text = ""
    @State private var selection = 0
    @State private var status: Status = .idle
    @State private var showDraft: Flow?

    private enum Status: Equatable {
        case idle
        case working(String)
        case done(String, Bool)          // message, success
        case refused(String)
    }

    @FocusState private var focused: Bool

    private var suggestions: [AnywhereCommand.Suggestion] {
        controller.resolverInstance().suggestions(recent: controller.recent, limit: 7)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            field
            if !text.isEmpty {
                preview
            }
            Divider().overlay(V.divider)
            if text.isEmpty {
                list
            }
            footer
        }
        .frame(width: 620, height: 420, alignment: .top)
        .background(V.background, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(V.border, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .onAppear {
            focused = true
            text = ""
            selection = 0
            status = .idle
        }
        .onReceive(NotificationCenter.default.publisher(for: .anywhereDidOpen)) { _ in
            focused = true
        }
        .background(
            Button("") { controller.dismiss() }
                .keyboardShortcut(.escape, modifiers: [])
                .opacity(0)
        )
        .vConfirm(
            "Create this flow?",
            message: draftMessage,
            confirm: "Save flow",
            isPresented: Binding(get: { showDraft != nil },
                                 set: { if !$0 { showDraft = nil } }),
            destructive: false
        ) {
            if let draft = showDraft {
                _ = flows.save(draft)
                status = .done("Saved “\(draft.name)” with \(draft.steps.count) steps. "
                               + "Run it from Flows, or say “run my \(draft.name) flow”.", true)
                showDraft = nil
                text = ""
            }
        }
    }

    private var draftMessage: String {
        guard let draft = showDraft else { return "" }
        return "“\(draft.name)” — " + draft.steps.map(\.title).joined(separator: " → ")
            + ". Saving stores it; nothing runs until you run it."
    }

    // MARK: - Field

    private var field: some View {
        HStack(spacing: V.Space.s) {
            Image(systemName: "sparkle")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(V.gold)
            TextField("Open Safari, set the volume to 30 percent, run my flow…", text: $text)
                .textFieldStyle(.plain)
                .font(.system(size: 16))
                .focused($focused)
                .onSubmit { submitCurrent() }
                .onChange(of: text) { _ in selection = 0; status = .idle }
            if case .working = status {
                ProgressView().controlSize(.small)
            }
        }
        .padding(.horizontal, V.Space.l)
        .padding(.vertical, 14)
    }

    /// Shows what submitting will actually do, before it happens — including
    /// that it will ask for approval.
    @ViewBuilder
    private var preview: some View {
        let resolution = controller.resolverInstance().resolve(text)
        HStack(spacing: V.Space.s) {
            Image(systemName: symbol(for: resolution.outcome))
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(V.textSecondary)
            Text(resolution.label)
                .font(V.Font.label)
                .foregroundStyle(V.text)
            if resolution.needsConfirmation {
                Text("asks first")
                    .font(V.Font.caption)
                    .foregroundStyle(V.gold)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(V.goldSoft, in: Capsule())
            }
            Spacer(minLength: 0)
            if case .conversation = resolution.outcome {
                Text("Enter sends this to the full window")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
            }
        }
        .padding(.horizontal, V.Space.l)
        .padding(.bottom, 8)
    }

    // MARK: - Suggestions

    private var list: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(suggestions.enumerated()), id: \.element.id) { index, item in
                        row(item, selected: index == selection)
                            .id(index)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                text = item.text
                                selection = index
                                submitCurrent()
                            }
                    }
                }
            }
            .onChange(of: selection) { new in
                // Respect reduced motion: scroll without animating when the
                // system asks for stillness.
                if V.motionAllowed {
                    withAnimation(.easeOut(duration: 0.12)) { proxy.scrollTo(new, anchor: .center) }
                } else {
                    proxy.scrollTo(new, anchor: .center)
                }
            }
        }
    }

    private func row(_ item: AnywhereCommand.Suggestion, selected: Bool) -> some View {
        HStack(spacing: V.Space.m) {
            Image(systemName: item.symbol)
                .font(.system(size: 12))
                .foregroundStyle(selected ? V.accent : V.textTertiary)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.text).font(V.Font.body).foregroundStyle(V.text)
                Text(item.detail).font(V.Font.caption).foregroundStyle(V.textTertiary)
            }
            Spacer(minLength: 0)
            if selected {
                Image(systemName: "arrow.turn.down.left")
                    .font(.system(size: 10))
                    .foregroundStyle(V.textTertiary)
            }
        }
        .padding(.horizontal, V.Space.l)
        .padding(.vertical, 7)
        .background(selected ? V.accentSoft : Color.clear)
    }

    // MARK: - Footer

    private var footer: some View {
        HStack(spacing: V.Space.s) {
            statusLine
            Spacer(minLength: V.Space.s)
            Button("Open Vantage") { controller.openMainWindow() }
                .buttonStyle(.plain)
                .font(V.Font.caption)
                .foregroundStyle(V.textSecondary)
            Text(controller.hotKey.current.displayString)
                .font(V.Font.mono)
                .foregroundStyle(V.textTertiary)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(V.fill, in: RoundedRectangle(cornerRadius: 4))
        }
        .padding(.horizontal, V.Space.l)
        .padding(.vertical, 9)
        .background(V.surfaceAlt)
    }

    @ViewBuilder
    private var statusLine: some View {
        switch status {
        case .idle:
            Text("↑↓ to choose · Enter to run · Esc to close")
                .font(V.Font.caption).foregroundStyle(V.textTertiary)
        case .working(let what):
            Text(what).font(V.Font.caption).foregroundStyle(V.textSecondary)
        case .done(let message, let ok):
            Label(message, systemImage: ok ? "checkmark.circle.fill" : "questionmark.circle.fill")
                .font(V.Font.caption)
                .foregroundStyle(ok ? V.accent : V.warning)
                .lineLimit(2)
        case .refused(let message):
            Label(message, systemImage: "hand.raised.fill")
                .font(V.Font.caption).foregroundStyle(V.textSecondary)
                .lineLimit(2)
        }
    }

    // MARK: - Keyboard

    /// Arrow keys move the selection. Done with a local monitor that is removed
    /// when the view goes away, because SwiftUI has no "arrow key" event on a
    /// TextField without swallowing the caret.
    private func handleKey(_ event: NSEvent) -> NSEvent? {
        guard text.isEmpty, !suggestions.isEmpty else { return event }
        switch Int(event.keyCode) {
        case kVK_DownArrow:
            selection = min(selection + 1, suggestions.count - 1)
            return nil
        case kVK_UpArrow:
            selection = max(selection - 1, 0)
            return nil
        default:
            return event
        }
    }

    // MARK: - Submitting

    private func submitCurrent() {
        if text.isEmpty, suggestions.indices.contains(selection) {
            text = suggestions[selection].text
        }
        submit()
    }

    private func submit() {
        let typed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !typed.isEmpty else { return }
        let resolution = controller.resolverInstance().resolve(typed)
        controller.remember(typed)

        switch resolution.outcome {
        case .conversation:
            // Not a Mac action: the full window answers it. The palette does not
            // pretend to be a chat.
            status = .working("Opening Vantage to answer…")
            controller.openMainWindow()
            NotificationCenter.default.post(name: .anywhereAskConversation,
                                            object: nil, userInfo: ["text": typed])

        case .explanation:
            status = .working("Opening Vantage to answer…")
            controller.openMainWindow()
            NotificationCenter.default.post(name: .anywhereAskConversation,
                                            object: nil, userInfo: ["text": typed])

        case .clarification(let question):
            status = .refused(question)

        case .action(let kind, let arguments):
            performAction(kind, arguments, label: resolution.label)

        case .plan(let steps):
            status = .working("Running \(steps.count) steps…")
            Task {
                let result = await control.runPlan(steps)
                report(result)
            }

        case .runFlow(let flow):
            status = .working("Running “\(flow.name)”…")
            Task {
                let run = await FlowRuntime.shared.engine.run(flow)
                if let pending = FlowRuntime.shared.engine.pendingApproval {
                    // A protected step: approvals live in the full window so the
                    // person sees the exact action, not a palette toast.
                    status = .refused("“\(pending.summary)” needs your approval. "
                                      + "Opening Vantage…")
                    FlowRuntime.shared.focusApproval()
                    controller.openMainWindow()
                    return
                }
                report(run: run, flow: flow)
            }

        case .describeFlow(let flow):
            status = .done(describe(flow), true)

        case .listFlows(let saved):
            if saved.isEmpty {
                status = .refused("You have no saved flows yet. Say “create a flow that "
                                  + "opens Safari” to make one.")
            } else {
                status = .done(saved.map { "\($0.name) (\($0.steps.count) steps)" }
                    .joined(separator: " · "), true)
            }

        case .draftFlow(let draft, let problems):
            if problems.isEmpty {
                showDraft = draft
            } else {
                // Incomplete drafts still open the editor-with-problems flow by
                // offering what could be built; nothing is saved yet.
                showDraft = draft
                status = .refused(problems.first ?? "Some steps could not be mapped.")
            }

        case .flowProblem(let message):
            status = .refused(message)

        // Phase 3. The palette can *save* a capture — that is the one new action
        // that is safe to complete here, because it changes nothing outside the
        // app's own inbox. Showing the inbox, the briefing or a document needs
        // the full window, so those hand off rather than pretending.
        case .capture(let item):
            saveCapture(item)

        case .showInbox:
            status = .working("Opening Vantage…")
            AppNavigation.shared.go(.capture)
            controller.openMainWindow()

        case .briefing:
            status = .working("Opening your briefing…")
            AppNavigation.shared.go(.briefing)
            controller.openMainWindow()

        case .openDocuments:
            status = .working("Opening Documents…")
            AppNavigation.shared.go(.documents)
            controller.openMainWindow()

        case .documentQuestion(let question):
            // The Documents screen owns the text; the palette never reads a file.
            status = .working("Opening Vantage to answer…")
            AppNavigation.shared.go(.documents, prefill: question)
            controller.openMainWindow()
        }
    }

    /// Save a resolved capture and say exactly what happened.
    ///
    /// The duplicate refusal is shown as itself ("already in your inbox") rather
    /// than a generic failure, because pressing Enter twice is the most likely
    /// cause and the person needs to know nothing was added.
    private func saveCapture(_ item: CaptureItem) {
        switch CaptureRuntime.shared.store.add(item) {
        case .success(let saved):
            var message = "Saved \(saved.kind.displayName.lowercased()) to your inbox."
            if saved.suggestedDue != nil {
                message += " It mentions a date — review it to create a task or reminder."
            }
            status = .done(message, true)
            text = ""
        case .failure(let error):
            status = .refused(error.localizedDescription)
        }
    }

    private func performAction(_ kind: MacActionKind, _ arguments: MacActionArguments,
                               label: String) {
        status = .working(label)
        Task {
            let result = await control.performForPalette(kind, arguments)
            switch result.status {
            case .verified:
                status = .done(result.message, true)
            case .unverified:
                status = .done(result.message, false)
            case .refused where control.pendingForPalette != nil:
                status = .refused("“\(label)” needs your approval. Opening Vantage…")
                controller.openMainWindow()
            case .refused:
                status = .refused(result.message)
            case .failed:
                status = .refused(result.message)
            case .cancelled:
                status = .refused(result.message)
            }
        }
    }

    private func report(_ result: MacActionResult?) {
        guard let result else { status = .refused("Nothing ran."); return }
        status = result.succeeded ? .done(result.message, true)
                                  : .done(result.message, result.status == .unverified)
    }

    private func report(run: FlowRun?, flow: Flow) {
        guard let run else { status = .refused("The flow did not run."); return }
        switch run.state {
        case .completed:
            status = .done("“\(flow.name)” finished: \(run.verifiedCount)/\(run.stepCount) steps.", true)
        case .completedWithErrors:
            status = .done("“\(flow.name)” finished with errors: \(run.stopReason)", false)
        case .cancelled:
            status = .refused("“\(flow.name)” was cancelled.")
        case .failed:
            status = .refused("“\(flow.name)” stopped: \(run.stopReason)")
        case .running:
            status = .refused("That flow is still waiting for approval.")
        }
    }

    private func describe(_ flow: Flow) -> String {
        var text = "“\(flow.name)” has \(flow.steps.count) steps: "
            + flow.steps.enumerated().map { "\($0.offset + 1). \($0.element.title)"
                + ($0.element.summary.isEmpty ? "" : " — \($0.element.summary)") }
                .joined(separator: "; ") + "."
        if !flow.description.isEmpty { text += " \(flow.description)" }
        return text
    }

    private func symbol(for outcome: AnywhereCommand.Outcome) -> String {
        switch outcome {
        case .action: return "bolt"
        case .plan: return "list.number"
        case .runFlow: return "arrow.triangle.2.circlepath"
        case .describeFlow: return "info.circle"
        case .draftFlow: return "square.and.pencil"
        case .listFlows: return "list.bullet.rectangle"
        case .explanation: return "text.bubble"
        case .clarification: return "questionmark.circle"
        case .conversation: return "bubble.left.and.bubble.right"
        case .flowProblem: return "exclamationmark.triangle"
        case .capture: return "tray.and.arrow.down"
        case .showInbox: return "tray.full"
        case .briefing: return "sun.max"
        case .openDocuments: return "doc.text"
        case .documentQuestion: return "doc.text.magnifyingglass"
        }
    }
}

extension Notification.Name {
    /// The palette asked the main window to answer something conversationally.
    static let anywhereAskConversation = Notification.Name("vantage.anywhere.ask")
}

// Carbon key codes used by the arrow-key monitor.
private let kVK_UpArrow = 0x7E
private let kVK_DownArrow = 0x7D