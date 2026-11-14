import SwiftUI
import AppKit

/// Control Mac — the native surface for real actions on this Mac.
///
/// Layout follows the product's own rule: what is *shown as available* is what
/// the current process can actually do. A section whose permission is missing
/// still appears, with why it needs the permission and how to grant it, because
/// hiding it would leave a person unable to fix it. No button here is
/// decorative: each one calls `MacControlCoordinator.perform`, which validates,
/// applies the risk policy and executes through the native service.
struct MacControlView: View {

    @ObservedObject private var control = MacControlCoordinator.shared

    @State private var command = ""
    @State private var lastResult: MacActionResult?
    @State private var showHistory = true
    @State private var appsExpanded = false
    @State private var volumeDraft: Double = 50
    @State private var volumeKnown = false
    @State private var folderName = "Downloads"
    @State private var appQuery = ""

    private let api = APIClient.shared

    private let examples = [
        "Open Safari",
        "Open my Downloads folder",
        "Set the volume to 30 percent",
        "Minimize the current window",
        "Which app is in the foreground?",
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.section) {
                header

                if let lastResult {
                    Notice(kind: tone(for: lastResult),
                           message: lastResult.message,
                           actionTitle: "Dismiss") {
                        self.lastResult = nil
                    }
                }

                commandEntry
                permissionsCard
                applicationSection
                windowSection
                systemSection
                folderSection
                if showHistory { recentSection }
                footnote
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 820)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("Control Mac")
        .task {
            control.refreshPermissions()
            control.refreshApplications()
            await readVolume()
        }
        .vConfirm(
            control.pending?.summary ?? "",
            message: "This changes something on your Mac. Nothing has run yet — confirm to "
                + "proceed, or Cancel to leave everything as it is.",
            confirm: "Run it",
            isPresented: Binding(
                get: { control.pending != nil },
                set: { if !$0 { control.cancelPending() } }),
            destructive: false
        ) {
            Task { lastResult = await control.confirmPending() }
        }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(title: "Control Mac",
                   subtitle: "Real actions on this Mac, performed natively with your permission.") {
            HStack(spacing: V.Space.s) {
                if control.isRunning {
                    ProgressView().controlSize(.small)
                    Button("Stop") { control.stop() }
                        .buttonStyle(VSecondaryButtonStyle())
                        .help("Stops a multi-step request between steps. A step already running finishes.")
                }
                Button {
                    control.refreshPermissions()
                    control.refreshApplications()
                    Task { await readVolume() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(control.isRunning)
            }
        }
    }

    // MARK: - Command entry

    private var commandEntry: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                Text("Ask for something").font(V.Font.cardTitle).foregroundStyle(V.text)
                HStack(spacing: V.Space.s) {
                    VField(placeholder: "Open Safari, set the volume to 30 percent…",
                           text: $command) {
                        submit()
                    }
                    Button("Run") { submit() }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(command.trimmingCharacters(in: .whitespaces).isEmpty
                                  || control.isRunning)
                }
                // Every example is a real request this parser handles; tapping
                // one fills the field rather than running something unseen.
                FlowRow(spacing: V.Space.xs) {
                    ForEach(examples, id: \.self) { example in
                        Button(example) { command = example }
                            .buttonStyle(VSecondaryButtonStyle())
                    }
                }
                if let progress = control.progress, progress.total > 1 {
                    Text("Step \(min(progress.completed + 1, progress.total)) of \(progress.total) — "
                         + progress.current)
                        .font(V.Font.caption).foregroundStyle(V.textSecondary)
                }
                Text("A request that needs a change will ask before it runs. "
                     + "Questions like “how do I open Safari” are answered, not executed.")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
            }
        }
    }

    private func submit() {
        let text = command.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !control.isRunning else { return }
        Task {
            lastResult = await control.runTyped(text)
            if lastResult?.status != .unverified || control.pending == nil { command = "" }
        }
    }

    // MARK: - Permissions

    private var permissionsCard: some View {
        SectionBlock(title: "Permissions") {
            Card(tone: control.permissions.accessibilityGranted == true ? .plain : .gold) {
                VStack(alignment: .leading, spacing: V.Space.s) {
                    HStack(spacing: V.Space.s) {
                        Circle()
                            .fill(control.permissions.accessibilityGranted == true
                                  ? V.accent : V.warning)
                            .frame(width: 7, height: 7)
                        Text("Accessibility — \(control.permissions.accessibilityLabel)")
                            .font(V.Font.label).foregroundStyle(V.text)
                        Spacer()
                        Button("Recheck") {
                            control.refreshPermissions()
                        }
                        .buttonStyle(VSecondaryButtonStyle())
                        .controlSize(.small)
                    }
                    if control.permissions.accessibilityGranted != true {
                        Text("Window control (move, minimise, close, list windows) needs "
                             + "Accessibility. Applications, folders, volume and system "
                             + "information work without it — those sections stay usable.")
                            .font(V.Font.body).foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                        HStack(spacing: V.Space.s) {
                            Button("Open System Settings") {
                                Task {
                                    lastResult = await control.perform(
                                        .openSystemSettings,
                                        { var a = MacActionArguments(); a.settingsPane = "accessibility"; return a }())
                                }
                            }
                            .buttonStyle(VSecondaryButtonStyle())
                            Button("Show macOS prompt") {
                                MacPermissionInspector.requestAccessibilityPrompt()
                            }
                            .buttonStyle(VSecondaryButtonStyle())
                            .help("Asks macOS to show its own consent dialog once.")
                        }
                    } else {
                        Text("Window actions are available. A target application that exposes "
                             + "no accessible windows will still report that honestly.")
                            .font(V.Font.body).foregroundStyle(V.textSecondary)
                    }
                    Divider().overlay(V.divider)
                    Text(control.permissions.automationNote)
                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    // MARK: - Applications

    private var applicationSection: some View {
        SectionBlock(title: "Applications") {
            Card {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    HStack(spacing: V.Space.s) {
                        VField(placeholder: "Application name", text: $appQuery)
                        Button("Open") { openTypedApp() }
                            .buttonStyle(VPrimaryButtonStyle())
                            .disabled(appQuery.trimmingCharacters(in: .whitespaces).isEmpty)
                        Button("Switch to") { switchTypedApp() }
                            .buttonStyle(VSecondaryButtonStyle())
                            .disabled(appQuery.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    HStack(spacing: V.Space.s) {
                        actionButton("Foreground app", symbol: "app.badge") {
                            await control.perform(.frontmostApp)
                        }
                        actionButton("Running apps", symbol: "list.bullet") {
                            await control.perform(.listRunningApps)
                        }
                        actionButton(appsExpanded ? "Hide apps" : "Show installed",
                                     symbol: "square.grid.2x2") {
                            appsExpanded.toggle()
                            return nil
                        }
                    }
                    if appsExpanded {
                        let names = control.installedApplications
                        if names.isEmpty {
                            Text("No applications were found in the standard folders.")
                                .font(V.Font.caption).foregroundStyle(V.textSecondary)
                        } else {
                            FlowRow(spacing: V.Space.xs) {
                                ForEach(names.prefix(60), id: \.self) { name in
                                    Button(name) {
                                        appQuery = name
                                    }
                                    .buttonStyle(VSecondaryButtonStyle())
                                }
                            }
                            Text("\(names.count) installed applications. Choosing one fills the "
                                 + "field; nothing launches until you press Open.")
                                .font(V.Font.caption).foregroundStyle(V.textTertiary)
                        }
                    }
                    if !control.installedApplications.isEmpty {
                        let running = control.lastRunningApplications
                        if !running.isEmpty {
                            Text("Running now: \(running.joined(separator: ", "))")
                                .font(V.Font.caption).foregroundStyle(V.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
        }
    }

    private func openTypedApp() {
        let name = appQuery.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        Task {
            lastResult = await control.perform(.openApplication,
                                               { var a = MacActionArguments(); a.application = name; return a }())
        }
    }

    private func switchTypedApp() {
        let name = appQuery.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        Task {
            lastResult = await control.perform(.activateApplication,
                                               { var a = MacActionArguments(); a.application = name; return a }())
        }
    }

    // MARK: - Windows

    private var windowSection: some View {
        let available = control.permissions.accessibilityGranted == true
        return SectionBlock(title: "Windows") {
            Card(tone: available ? .plain : .ivory) {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    Text("These act on the frontmost window, or on a named application's window.")
                        .font(V.Font.caption).foregroundStyle(V.textSecondary)
                    FlowRow(spacing: V.Space.s) {
                        actionButton("List windows", symbol: "rectangle.on.rectangle",
                                     enabled: available, args: windowArgs()) {
                            await control.perform(.listWindows, windowArgs())
                        }
                        actionButton("Minimise", symbol: "minus.rectangle",
                                     enabled: available, args: windowArgs()) {
                            await control.perform(.minimizeWindow, windowArgs())
                        }
                        actionButton("Left half", symbol: "rectangle.lefthalf.filled",
                                     enabled: available) {
                            await control.perform(.arrangeWindow, sideArgs(.left))
                        }
                        actionButton("Right half", symbol: "rectangle.righthalf.filled",
                                     enabled: available) {
                            await control.perform(.arrangeWindow, sideArgs(.right))
                        }
                        actionButton("Maximise", symbol: "arrow.up.left.and.arrow.down.right",
                                     enabled: available) {
                            await control.perform(.arrangeWindow, sideArgs(.maximise))
                        }
                        actionButton("Close…", symbol: "xmark.rectangle",
                                     enabled: available, style: .secondary) {
                            await control.perform(.closeWindow, windowArgs())
                        }
                    }
                    if !available {
                        Text("Unavailable: Accessibility is not granted to Vantage. The buttons "
                             + "are disabled rather than pretending to move a window.")
                            .font(V.Font.caption).foregroundStyle(V.warning)
                    } else {
                        Text("Closing can discard unsaved work; Vantage asks first, and the "
                             + "application still shows its own save dialog.")
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    }
                }
            }
        }
    }

    private func windowArgs() -> MacActionArguments {
        var args = MacActionArguments()
        let name = appQuery.trimmingCharacters(in: .whitespaces)
        // Empty means "the frontmost window", which the executor resolves itself.
        args.application = name
        return args
    }

    private func sideArgs(_ side: MacWindowSide) -> MacActionArguments {
        var args = windowArgs()
        args.side = side
        return args
    }

    // MARK: - System

    private var systemSection: some View {
        SectionBlock(title: "System") {
            Card {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    HStack(spacing: V.Space.m) {
                        Image(systemName: "speaker.wave.2.fill")
                            .foregroundStyle(V.textSecondary)
                        Slider(value: $volumeDraft, in: 0...100, step: 1) { editing in
                            if !editing { Task { await applyVolume() } }
                        }
                        .frame(maxWidth: 280)
                        Text(volumeKnown ? "\(Int(volumeDraft))%" : "—")
                            .font(V.Font.mono).foregroundStyle(V.textSecondary)
                            .frame(width: 44, alignment: .trailing)
                        Button("Set") { Task { await applyVolume() } }
                            .buttonStyle(VSecondaryButtonStyle())
                        Button("Read") { Task { await readVolume() } }
                            .buttonStyle(VSecondaryButtonStyle())
                    }
                    FlowRow(spacing: V.Space.s) {
                        actionButton("Mute", symbol: "speaker.slash.fill") {
                            await control.perform(.setMuted,
                                                  { var a = MacActionArguments(); a.flag = true; return a }())
                        }
                        actionButton("Unmute", symbol: "speaker.wave.3.fill") {
                            await control.perform(.setMuted,
                                                  { var a = MacActionArguments(); a.flag = false; return a }())
                        }
                        actionButton("System & display", symbol: "display") {
                            await control.perform(.systemInfo)
                        }
                        actionButton("Open Sound settings", symbol: "gearshape",
                                     style: .secondary) {
                            await control.perform(.openSystemSettings,
                                                  { var a = MacActionArguments(); a.settingsPane = "sound"; return a }())
                        }
                    }
                    Text("Volume is read and written through CoreAudio on the current output "
                         + "device, and read back after writing. Displays, battery and OS "
                         + "version come from AppKit and IOKit.")
                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func readVolume() async {
        let result = await control.perform(.getVolume)
        lastResult = result
        if let percent = result.details["percent"], let value = Double(percent) {
            volumeDraft = value
            volumeKnown = true
        }
    }

    private func applyVolume() async {
        var args = MacActionArguments()
        args.percent = Int(volumeDraft.rounded())
        lastResult = await control.perform(.setVolume, args)
        if let percent = lastResult?.details["percent"], let value = Double(percent) {
            volumeDraft = value
        }
    }

    // MARK: - Folders

    private var folderSection: some View {
        SectionBlock(title: "Files and folders") {
            Card {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    HStack(spacing: V.Space.s) {
                        Picker("", selection: $folderName) {
                            ForEach(["Downloads", "Documents", "Desktop", "Pictures", "Music"], id: \.self) {
                                Text($0).tag($0)
                            }
                        }
                        .labelsHidden()
                        .frame(width: 150)
                        Button("Open folder") { Task { await openFolder() } }
                            .buttonStyle(VPrimaryButtonStyle())
                    }
                    Text("Vantage opens these folders in Finder. It does not read their "
                         + "contents from here, and it will not modify a protected location "
                         + "or a credentials store.")
                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func openFolder() async {
        var args = MacActionArguments()
        args.path = folderName
        lastResult = await control.perform(.openFolder, args)
    }

    // MARK: - History

    private var recentSection: some View {
        SectionBlock(title: "Recent actions", trailing: AnyView(
            HStack(spacing: V.Space.s) {
                if !control.history.isEmpty {
                    Button("Clear") { control.clearHistory() }
                        .buttonStyle(VSecondaryButtonStyle())
                        .controlSize(.small)
                }
            }
        )) {
            Card {
                if control.history.isEmpty {
                    EmptyState(symbol: "clock",
                               title: "Nothing yet",
                               message: "Actions you run appear here with their real outcome, "
                                   + "the target and the risk class. Nothing is recorded until "
                                   + "you do something.")
                } else {
                    VStack(spacing: 0) {
                        ForEach(control.history) { entry in
                            historyRow(entry)
                            if entry.id != control.history.last?.id {
                                Divider().overlay(V.divider)
                            }
                        }
                    }
                }
            }
        }
    }

    private func historyRow(_ entry: MacHistoryEntry) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Image(systemName: symbol(for: entry.status))
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(colour(for: entry.status))
                .frame(width: 14)
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.action.displayName)
                    .font(V.Font.label).foregroundStyle(V.text)
                Text(entry.message)
                    .font(V.Font.caption).foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: V.Space.s)
            VStack(alignment: .trailing, spacing: 2) {
                Text(entry.target)
                    .font(V.Font.mono).foregroundStyle(V.textTertiary)
                    .lineLimit(1).truncationMode(.middle)
                Text("\(entry.risk.displayName) · \(entry.at.formatted(date: .omitted, time: .shortened))")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
            }
        }
        .padding(.vertical, V.Space.s)
    }

    private var footnote: some View {
        Text("Mac control runs inside this application, not on the backend. A request from "
             + "the phone or the model can read, and can run the low-risk actions it was "
             + "explicitly asked for; it cannot close your documents or change your files "
             + "without you at this Mac.")
            .font(V.Font.caption).foregroundStyle(V.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Small builders

    private enum ButtonStyleKind { case primary, secondary }

    @ViewBuilder
    private func actionButton(_ title: String, symbol: String,
                              enabled: Bool = true,
                              args: MacActionArguments? = nil,
                              style: ButtonStyleKind = .secondary,
                              action: @escaping () async -> MacActionResult?) -> some View {
        Button {
            Task { lastResult = await action() }
        } label: {
            Label(title, systemImage: symbol)
        }
        .buttonStyle(style == .primary ? AnyButtonStyle(VPrimaryButtonStyle())
                                       : AnyButtonStyle(VSecondaryButtonStyle()))
        // One line, never clipped: a wrapped chip reads as broken, and the
        // grid column is sized from the widest label.
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
        .disabled(!enabled || control.isRunning)
    }

    private func tone(for result: MacActionResult) -> Notice.Kind {
        switch result.status {
        case .verified: return .success
        case .unverified: return .warning
        case .refused: return .info
        case .failed: return .error
        case .cancelled: return .info
        }
    }

    private func symbol(for status: MacActionResult.Status) -> String {
        switch status {
        case .verified: return "checkmark.circle.fill"
        case .unverified: return "questionmark.circle.fill"
        case .refused: return "hand.raised.fill"
        case .failed: return "xmark.octagon.fill"
        case .cancelled: return "slash.circle.fill"
        }
    }

    private func colour(for status: MacActionResult.Status) -> Color {
        switch status {
        case .verified: return V.accent
        case .unverified: return V.warning
        case .refused: return V.textTertiary
        case .failed: return V.danger
        case .cancelled: return V.textTertiary
        }
    }
}

/// A simple wrapping row for chips and quick actions.
private struct FlowRow<Content: View>: View {
    var spacing: CGFloat = 8
    @ViewBuilder var content: () -> Content

    var body: some View {
        // Each chip must fit its own label. The previous adaptive grid used a
        // fixed 130pt minimum, which clipped "Show installed" and wrapped
        // "Foreground app" — a disabled-looking button with cut text. Growing
        // the column to fit the widest chip keeps one row when it can and
        // wraps only when it genuinely must.
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: spacing,
                                     alignment: .leading)],
                  alignment: .leading, spacing: spacing) {
            content()
        }
    }
}