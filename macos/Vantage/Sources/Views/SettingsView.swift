import SwiftUI
import AppKit
import UniformTypeIdentifiers

/// Application settings: backend connection, sessions, privacy, voice, about.
///
/// The page has two levels of navigation and one fixed element:
///
/// * the **AI Voice Responses control is pinned above everything else** and is
///   never behind a section. It used to live only in the Voice tab, and the
///   person looking for "where do I turn the voice off" never found it — a
///   setting behind a tab they had no reason to open. It now sits at the top
///   of the page, visible on every section without scrolling;
/// * a compact segmented picker switches between the five sections;
/// * each section is drawn with the shared design system (`V` tokens, `Card`,
///   `SectionBlock`, `DetailRow`, `StatusPill`, `Notice`).
struct SettingsView: View {

    /// Observe the shared manager — do NOT own it with @StateObject (see ContentView:
    /// a view-owned @StateObject releases the shared singleton when the view goes away,
    /// which cancelled the backend's health polling).
    @ObservedObject private var backend = BackendManager.shared
    @State private var section: SettingsTab = .connection

    var body: some View {
        VStack(spacing: 0) {
            // The flagship control and the section picker. Both stay put while
            // a section scrolls, so "where is the voice switch" is never a
            // scroll away — and this block is capped to the page width so it
            // lines up with the cards below it.
            VStack(spacing: V.Space.m) {
                AIVoiceHeader()
                SettingsSectionPicker(selection: $section)
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.top, V.Space.l)
            .padding(.bottom, V.Space.m)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity)

            Rectangle()
                .fill(V.divider)
                .frame(height: 1)

            ScrollView {
                sectionContent
                    .padding(.horizontal, V.Space.gutter)
                    .padding(.top, V.Space.xl)
                    .padding(.bottom, V.Space.xxl)
                    .frame(maxWidth: 760)
                    .frame(maxWidth: .infinity, alignment: .top)
            }
        }
        .background(V.background)
        .navigationTitle("Settings")
    }

    @ViewBuilder
    private var sectionContent: some View {
        switch section {
        case .connection: ConnectionSettings(backend: backend)
        case .sessions:   SessionSettings()
        case .privacy:    PrivacySettings()
        case .voice:      VoiceSettings()
        case .about:      AboutSettings()
        }
    }
}

// MARK: - Sections

enum SettingsTab: String, CaseIterable, Identifiable {
    case connection, sessions, privacy, voice, about

    var id: String { rawValue }

    var title: String {
        switch self {
        case .connection: return "Connection"
        case .sessions:   return "Sessions"
        case .privacy:    return "Privacy"
        case .voice:      return "Voice"
        case .about:      return "About"
        }
    }

    var symbol: String {
        switch self {
        case .connection: return "network"
        case .sessions:   return "person.2.fill"
        case .privacy:    return "lock.fill"
        case .voice:      return "speaker.wave.2.fill"
        case .about:      return "info.circle"
        }
    }
}

/// The section switcher. A segmented control rather than a second sidebar:
/// the app shell already has one, and the detail pane is ~670pt wide at the
/// default window size, where a second vertical rail would squeeze the cards.
///
/// Painted from tokens instead of a stock `Picker` so the selected segment uses
/// the surface/accent language of the rest of the app. Every segment is a real
/// focusable button, so it is reachable with the keyboard.
private struct SettingsSectionPicker: View {
    @Binding var selection: SettingsTab

    var body: some View {
        HStack(spacing: V.Space.xxs) {
            ForEach(SettingsTab.allCases) { tab in
                let isSelected = selection == tab
                Button {
                    guard !isSelected else { return }
                    V.animate { selection = tab }
                } label: {
                    HStack(spacing: V.Space.xs + 1) {
                        Image(systemName: tab.symbol)
                            .font(.system(size: 11, weight: .semibold))
                        Text(tab.title)
                            .font(V.Font.label)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                    }
                    .foregroundStyle(isSelected ? V.text : V.textSecondary)
                    .padding(.horizontal, V.Space.s + 1)
                    .padding(.vertical, 5)
                    .frame(maxWidth: .infinity)
                    .background(
                        RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                            .fill(isSelected ? V.surface : Color.clear)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                            .strokeBorder(isSelected ? V.border : Color.clear, lineWidth: 1)
                    )
                    .contentShape(RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
                }
                .buttonStyle(.plain)
                .help("Show \(tab.title) settings")
                .accessibilityLabel(tab.title)
                .accessibilityAddTraits(isSelected ? [.isSelected] : [])
            }
        }
        .padding(V.Space.xs)
        .background(
            RoundedRectangle(cornerRadius: V.Radius.control + 3, style: .continuous)
                .fill(V.fill)
        )
        .overlay(
            RoundedRectangle(cornerRadius: V.Radius.control + 3, style: .continuous)
                .strokeBorder(V.border.opacity(0.6), lineWidth: 1)
        )
    }
}

// MARK: - AI voice header

/// The always-visible AI-voice switch.
///
/// One preference, one binding: `VoiceService.shared.speakReplies`. Turning it
/// off stops any utterance already playing (the property's `didSet` does that),
/// so this control is a real mute rather than a label. The card itself tints
/// when speech is on, so the state is legible from across the room and not only
/// from the switch position.
struct AIVoiceHeader: View {
    @ObservedObject private var voice = VoiceService.shared

    var body: some View {
        Card(tone: voice.speakReplies ? .accent : .plain) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(alignment: .center, spacing: V.Space.m) {
                    ZStack {
                        RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                            .fill(voice.speakReplies ? V.accent : V.fill)
                        Image(systemName: voice.speakReplies
                              ? "speaker.wave.2.fill" : "speaker.slash.fill")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(voice.speakReplies ? Color.white : V.textSecondary)
                    }
                    .frame(width: 32, height: 32)
                    .accessibilityHidden(true)

                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: V.Space.s) {
                            Text("AI Voice Responses")
                                .font(V.Font.cardTitle)
                                .foregroundStyle(V.text)
                            StatusPill(kind: voice.speakReplies ? .connected : .idle,
                                       text: voice.speakReplies ? "On" : "Off")
                        }
                        Text(voice.speakReplies
                             ? "On — Vantage reads its answers aloud on this Mac."
                             : "Off — Vantage answers on screen and says nothing aloud.")
                            .font(V.Font.body)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Spacer(minLength: V.Space.s)

                    Toggle("AI Voice Responses", isOn: $voice.speakReplies)
                        .labelsHidden()
                        .toggleStyle(.switch)
                        .tint(V.accent)
                        .accessibilityLabel("AI Voice Responses")
                        .help("Read Vantage's answers aloud on this Mac")
                }

                // While a reply is audible, say so and offer the stop. The row
                // sits under the switch rather than beside it so it survives a
                // narrow window without squeezing the label.
                if voice.isSpeaking {
                    HStack(spacing: V.Space.s) {
                        Image(systemName: "waveform")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(V.accent)
                        Text("Speaking…")
                            .font(V.Font.label)
                            .foregroundStyle(V.textSecondary)
                        Spacer(minLength: V.Space.s)
                        Button("Stop") { voice.stopSpeaking() }
                            .buttonStyle(VSecondaryButtonStyle())
                            .help("Stop the reply that is being spoken")
                    }
                    .padding(.leading, 32 + V.Space.m)
                    .accessibilityElement(children: .contain)
                    .accessibilityLabel("Speaking now")
                }
            }
        }
    }
}

// MARK: - Voice

/// Spoken replies, and a way to hear what they will sound like.
struct VoiceSettings: View {
    @ObservedObject private var voice = VoiceService.shared

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.section) {
            PageHeader(
                title: "Voice",
                subtitle: "Spoken replies use the system voice on this Mac. Nothing is sent anywhere to be spoken."
            )

            SectionBlock(title: "Spoken replies") {
                Card {
                    VStack(alignment: .leading, spacing: V.Space.m) {
                        // The same binding as the pinned control at the top of
                        // Settings: one preference, shown in both places it is
                        // looked for.
                        VToggleRow(
                            title: "AI Voice Responses",
                            detail: voice.speakReplies
                                ? "On — answers are spoken as they arrive, using the system voice."
                                : "Off — answers stay on screen only. Turn this on if you want Vantage to read them out.",
                            isOn: $voice.speakReplies
                        )

                        Divider().overlay(V.divider)

                        HStack(alignment: .top, spacing: V.Space.m) {
                            // A one-off sample. It deliberately bypasses the
                            // gate, so it can be heard while the setting is off
                            // — but it never changes the setting.
                            Button("Play a sample (one-off)") { voice.previewVoice() }
                                .buttonStyle(VSecondaryButtonStyle())
                                .help("Hear the system voice once")
                            Text("Plays once. Does not change the AI Voice Responses setting.")
                                .font(V.Font.caption)
                                .foregroundStyle(V.textTertiary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }

            if let error = voice.errorMessage {
                Notice(kind: .warning, message: error, actionTitle: "Dismiss") {
                    voice.errorMessage = nil
                }
            }

            Text("The switch pinned at the top of Settings is this same setting and stays visible on "
                 + "every section. The labelled \"AI voice\" control beside the Command Center's message "
                 + "field switches it too, so it can be muted mid-conversation without opening Settings.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Connection

struct ConnectionSettings: View {
    @ObservedObject var backend: BackendManager
    @State private var tokenLoaded = APIClient.shared.hasToken
    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.section) {
            PageHeader(
                title: "Connection",
                subtitle: "How this Mac reaches the Vantage backend that runs on it."
            )

            SectionBlock(title: "Backend") {
                Card {
                    VStack(alignment: .leading, spacing: V.Space.m) {
                        HStack(spacing: V.Space.s) {
                            StatusPill(kind: stateKind, text: stateText)
                            Spacer(minLength: V.Space.s)
                            Button("Connect") { Task { await backend.connect() } }
                                .buttonStyle(VPrimaryButtonStyle())
                                .disabled(isBusy || isRunning)
                                .help(isRunning
                                      ? "The backend is already running"
                                      : "Look for a running backend, then start one if there is none")
                            Button("Refresh") { Task { await backend.refresh() } }
                                .buttonStyle(VSecondaryButtonStyle())
                                .disabled(isBusy)
                                .help("Re-read the backend's health and identity reports")
                        }

                        statusDetail

                        Divider().overlay(V.divider)

                        // The URL is read from the manager rather than typed in,
                        // so an internal port override can never be shown as the
                        // wrong address (the fixed string used to say 8765 while
                        // the manager probed a different port).
                        DetailRow(label: "URL", value: backend.backendURL, monospaced: true)
                        if let h = backend.health {
                            DetailRow(label: "Engine",
                                      value: "\(h.tools ?? 0) tools · \(h.agents?.count ?? 0) agents")
                            DetailRow(label: "AI provider", value: h.ai_provider ?? "none")
                            DetailRow(label: "LLM", value: h.llm_source ?? "unknown")
                        }
                        // What the process on the port says it is
                        // (`/health/identity`). The rows are the facts the
                        // backend reported — not inferred from the app — so a
                        // stale build, a second checkout, or an older build
                        // that predates the route is visible here rather than
                        // hidden behind a green "Connected".
                        if let id = backend.identity {
                            Divider().overlay(V.divider)
                            DetailRow(label: "Version", value: id.app_version ?? "unknown")
                            DetailRow(label: "Revision", value: id.revision ?? "unknown")
                            DetailRow(label: "Branch", value: id.branch ?? "unknown")
                            DetailRow(label: "Instance",
                                      value: "\(id.instance_id.prefix(8))…"
                                        + (id.pid.map { " · pid \($0)" } ?? ""),
                                      monospaced: true)
                            DetailRow(label: "Started", value: startedText(id.started_at))
                            if let root = id.source_root {
                                DetailRow(label: "Source folder", value: root, monospaced: true)
                            }
                        }
                    }
                }
            }

            SectionBlock(title: "Vantage Anywhere") { anywhereCard }

            SectionBlock(title: "Backend lifecycle") {
                lifecycleCard
            }

            SectionBlock(title: "API token") {
                tokenCard
            }
        }
        .task { refreshTokenState() }
        .task { await backend.refreshLifecycle() }
        .vConfirm(
            lifecycleAction.confirmTitle,
            message: lifecycleAction.confirmMessage(port: backend.lifecyclePort),
            confirm: lifecycleAction.confirmButton,
            isPresented: $confirmingLifecycleAction
        ) {
            Task { await backend.migrateOrRestart() }
        }
    }

    // MARK: Lifecycle

    @State private var confirmingLifecycleAction = false

    /// The recovery action the script's verdict permits — or `.none` when the
    /// port is not provably ours (no button is offered for those states).
    private enum LifecycleAction: Equatable {
        case none
        case start
        case restart
        case migrate

        var buttonTitle: String {
            switch self {
            case .none: return ""
            case .start: return "Start Backend"
            case .restart: return "Restart Backend…"
            case .migrate: return "Upgrade Legacy Backend…"
            }
        }

        var confirmTitle: String {
            switch self {
            case .none, .start: return ""
            case .restart: return "Restart the backend?"
            case .migrate: return "Upgrade the legacy backend?"
            }
        }

        var confirmButton: String {
            switch self {
            case .none, .start: return ""
            case .restart: return "Restart"
            case .migrate: return "Upgrade"
            }
        }

        /// Names the port and exactly what will happen. The script re-proves
        /// ownership before signalling anything.
        func confirmMessage(port: Int) -> String {
            switch self {
            case .none, .start:
                return ""
            case .restart:
                return "This stops the Vantage backend on port \(port) (proven to be this checkout's) "
                    + "and starts the current build. In-flight requests will fail and the app "
                    + "reconnects when the new instance is up."
            case .migrate:
                return "This stops the older Vantage backend on port \(port) and starts the current "
                    + "build in its place. It runs only because the process was proven to belong to "
                    + "this checkout; nothing else is signalled."
            }
        }

        var needsConfirmation: Bool { self == .restart || self == .migrate }

        static func derive(from status: BackendLifecycle.Status?) -> LifecycleAction {
            guard let status else { return .none }
            switch status.kind {
            case .free: return .start
            case .ours: return status.stale == true ? .restart : .none
            case .legacy: return status.migratable ? .migrate : .none
            case .foreign, .otherVantageCheckout, .unknown: return .none
            }
        }
    }

    private var lifecycleAction: LifecycleAction {
        LifecycleAction.derive(from: backend.lifecycleState)
    }

    private var lifecycleCard: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                HStack(spacing: V.Space.s) {
                    DetailRow(label: "Backend state",
                              value: backend.lifecycleState.map { $0.kind.displayName } ?? "not checked",
                              tone: lifecycleTone)
                    Spacer(minLength: V.Space.s)
                    Button("Check Again") {
                        Task {
                            await backend.refreshLifecycle()
                            await backend.refresh()
                        }
                    }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(backend.isLifecycleBusy)
                    .help("Run backend_lifecycle.py status and re-read health and identity")

                    if lifecycleAction != .none {
                        Button(lifecycleAction.buttonTitle) {
                            if lifecycleAction.needsConfirmation {
                                confirmingLifecycleAction = true
                            } else {
                                Task { await backend.migrateOrRestart() }
                            }
                        }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(backend.isLifecycleBusy)
                        .help(lifecycleAction.buttonTitle)
                    }
                }

                // The facts behind the verdict: what the script actually saw.
                if let status = backend.lifecycleState {
                    if let pid = status.pid {
                        DetailRow(label: "Listener", value: "pid \(pid)", monospaced: true)
                    }
                    DetailRow(label: "Code age",
                              value: status.stale == true ? "older than disk"
                                   : (status.stale == false ? "current" : "unknown"))
                    if let note = status.code?.note, !note.isEmpty {
                        DetailRow(label: "Note", value: note)
                    }
                    if status.kind == .legacy, let evidence = status.legacy {
                        DetailRow(label: "Evidence", value: evidence.reason ?? "unknown")
                        if let cwd = evidence.cwd, !cwd.isEmpty {
                            DetailRow(label: "Working dir", value: cwd, monospaced: true)
                        }
                    }
                }

                // A port owned by something this app cannot prove is its own:
                // state it plainly, say why nothing is offered, and give the
                // exact command a person can run themselves.
                if let status = backend.lifecycleState,
                   status.kind == .foreign || status.kind == .otherVantageCheckout || status.kind == .unknown {
                    Notice(kind: .error, message: BackendManager.refusal(for: status, port: backend.lifecyclePort))
                }

                if let error = backend.lifecycleError {
                    Notice(kind: .error, message: error, actionTitle: "Retry") {
                        Task { await backend.refreshLifecycle() }
                    }
                }

                if backend.isLifecycleBusy {
                    LoadingRow(text: backend.lifecycleBusyLabel ?? "Working…")
                } else if let outcome = backend.lastLifecycleOutcome {
                    Notice(kind: lifecycleOutcomeKind(outcome), message: outcome)
                }
            }
        }
    }

    private var lifecycleTone: Color? {
        switch backend.lifecycleState?.kind {
        case .ours: return V.accent
        case .free: return V.textSecondary
        case .legacy: return V.warning
        case .foreign, .otherVantageCheckout, .unknown: return V.danger
        case nil: return nil
        }
    }

    /// The script's refusals start with "Refusing" / contain "will not stop";
    /// a successful action names a new instance. Anything else is an error.
    private func lifecycleOutcomeKind(_ text: String) -> Notice.Kind {
        let lower = text.lowercased()
        if lower.contains("refus") || lower.contains("will not stop") || lower.contains("could not")
            || lower.contains("did not finish") || lower.contains("no status") {
            return .error
        }
        if lower.contains("no restart needed") {
            return .info
        }
        return .success
    }

    // MARK: State

    private var isBusy: Bool {
        switch backend.state {
        case .checking, .starting: return true
        default: return false
        }
    }

    private var isRunning: Bool {
        backend.state == .running
    }

    private var stateKind: StatusKind {
        // A stale backend is still a running backend: the app can talk to it,
        // so the pill is pending (amber) rather than the red of a dead one.
        if backend.stale && backend.state == .running { return .pending }
        switch backend.state {
        case .unknown:   return .setup
        case .checking:  return .setup
        case .running:   return .connected
        case .starting:  return .pending
        case .stopped:   return .idle
        case .error:     return .error
        }
    }

    private var stateText: String {
        if backend.stale && backend.state == .running { return "Connected — restart needed" }
        switch backend.state {
        case .unknown:  return "Unknown"
        case .checking: return "Checking…"
        case .running:  return "Connected"
        case .starting: return "Starting…"
        case .stopped:  return "Disconnected"
        case .error:    return "Error"
        }
    }

    @ViewBuilder
    private var statusDetail: some View {
        // A stale backend is `.running` with a warning: the app is usable, so
        // the warning leads (with the restart command), and the ordinary
        // running state is shown after it.
        if let warning = backend.warning {
            Notice(kind: .warning, message: warning)
        }
        switch backend.state {
        case .checking:
            LoadingRow(text: "Looking for a running backend…")
        case .starting:
            LoadingRow(text: "Starting the backend…")
        case .running:
            if backend.health == nil {
                Text("Connected, but the health details have not loaded yet. Use Refresh.")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textTertiary)
            }
        case .error(let message):
            Notice(kind: .error, message: message)
        case .stopped(let reason):
            Text(reason.isEmpty
                 ? "The backend is not running on this Mac. Connect starts it."
                 : "The backend is not running (\(reason)). Connect starts it.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
        case .unknown:
            Text("Not checked yet. Connect looks for a running backend, then starts one if there is none.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
        }
    }

    /// The backend's reported start time, in the local zone. An absent or
    /// implausible value (a pre-2001 epoch from a misbuilt package) is shown
    /// as unknown rather than as a date that never happened.
    private func startedText(_ epoch: Double?) -> String {
        guard let epoch, epoch > 946684800 else { return "unknown" } // 2000-01-01
        let date = Date(timeIntervalSince1970: epoch)
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f.string(from: date)
    }

    // MARK: Token

    // MARK: - Vantage Anywhere

    /// The global shortcut, configurable and with its *real* registration state.
    ///
    /// The state shown is what Carbon reported, not what was asked for: a
    /// shortcut another app already owns renders as "not registered" with the
    /// reason, so nobody is told a shortcut works when it does not.
    @ViewBuilder
    private var anywhereCard: some View {
        let controller = AnywhereController.shared
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                HStack(spacing: V.Space.s) {
                    Text("Global shortcut").font(V.Font.label).foregroundStyle(V.textSecondary)
                    Spacer()
                    ShortcutRecorder(current: controller.hotKey.current) { spec in
                        _ = controller.updateShortcut(spec)
                    }
                }
                HStack(spacing: V.Space.s) {
                    Circle()
                        .fill(controller.hotKey.isActive ? V.accent : V.warning)
                        .frame(width: 7, height: 7)
                    Text(controller.hotKey.statusText)
                        .font(V.Font.caption)
                        .foregroundStyle(controller.hotKey.isActive ? V.textSecondary : V.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: V.Space.s) {
                    Button("Test it") { controller.show() }
                        .buttonStyle(VSecondaryButtonStyle())
                    Button("Reset to \u{2325}\u{2423}") {
                        _ = controller.updateShortcut(.defaultShortcut)
                    }
                    .buttonStyle(VSecondaryButtonStyle())
                }
                Text("Vantage Anywhere lets you ask for something while you are in another "
                     + "app. It uses the same permission, risk and approval rules as the "
                     + "Vantage window: a step that needs approval still asks.")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var tokenCard: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                HStack(spacing: V.Space.s) {
                    StatusPill(kind: tokenLoaded ? .connected : .error,
                               text: tokenLoaded ? "Token loaded" : "No token found")
                    Spacer(minLength: V.Space.s)
                    // The presence of the token was read once at launch and
                    // never again, so a token created (or removed) while the
                    // app was open was reported wrongly until relaunch.
                    Button("Check again") { refreshTokenState() }
                        .buttonStyle(VQuietButtonStyle())
                        .help("Re-read the token file from disk")
                }

                DetailRow(label: "File", value: "~/.jarvis-forge/api_token", monospaced: true)

                Text(tokenLoaded
                     ? "Requests are authenticated with this token. Only whether it is present is shown here — never the token itself."
                     : "Without it the backend rejects every request. Only whether the file is present is shown here — never its contents.")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func refreshTokenState() {
        // Re-reading also clears a token that was deleted on disk; a cached
        // value would keep claiming "loaded" long after it stopped being true.
        _ = api.refreshToken()
        tokenLoaded = api.hasToken
    }
}

// MARK: - Sessions

struct SessionSettings: View {
    @State private var sessions: [APIModels.SessionInfo] = []
    @State private var isLoading = true
    @State private var revokeTarget: APIModels.SessionInfo?
    @State private var revokeError: String?
    @State private var loadError: String?
    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.section) {
            PageHeader(
                title: "Sessions",
                subtitle: "Every device signed in to this backend. Revoking one signs it out immediately."
            ) {
                Button("Refresh") { Task { await loadSessions() } }
                    .buttonStyle(VQuietButtonStyle())
                    .disabled(isLoading)
                    .help("Re-read the active sessions")
            }

            SectionBlock(title: "Active sessions") {
                Card {
                    if isLoading {
                        LoadingRow(text: "Loading active sessions…")
                    } else if let loadError {
                        Notice(kind: .error, message: loadError, actionTitle: "Retry") {
                            Task { await loadSessions() }
                        }
                    } else if sessions.isEmpty {
                        // Only reachable after a successful read, so this is a
                        // claim the screen is allowed to make.
                        VStack(alignment: .leading, spacing: V.Space.xs) {
                            Text("No active sessions.")
                                .font(V.Font.body)
                                .foregroundStyle(V.text)
                            Text("Nothing is signed in to this backend right now.")
                                .font(V.Font.caption)
                                .foregroundStyle(V.textTertiary)
                        }
                    } else {
                        VStack(alignment: .leading, spacing: V.Space.s) {
                            ForEach(sessions.indices, id: \.self) { index in
                                if index > 0 { Divider().overlay(V.divider) }
                                sessionRow(sessions[index])
                            }
                        }
                    }
                }
            }

            Text("A revoked device must pair again before it can reconnect.")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
        }
        .task { await loadSessions() }
        .alert(
            "Revoke this session?",
            isPresented: Binding(
                get: { revokeTarget != nil },
                set: { if !$0 { revokeTarget = nil } }
            )
        ) {
            Button("Revoke", role: .destructive) {
                if let target = revokeTarget {
                    revokeTarget = nil
                    // The server names a session `session_ref`. The previous
                    // code read a `ref` field that does not exist, so it
                    // revoked with an empty path -- `DELETE /auth/sessions/`
                    // -- and reported success either way.
                    if let ref = target.session_ref, !ref.isEmpty {
                        Task { await revoke(ref) }
                    } else {
                        revokeError = "This session has no reference, so it cannot be revoked."
                    }
                }
            }
            Button("Cancel", role: .cancel) { revokeTarget = nil }
        } message: {
            Text("Devices using this session stop working immediately. They must pair again to reconnect.")
        }
        .alert("Revoke failed", isPresented: Binding(
            get: { revokeError != nil },
            set: { if !$0 { revokeError = nil } }
        )) {
            Button("OK", role: .cancel) { }
        } message: {
            Text(revokeError ?? "")
        }
    }

    private func sessionRow(_ s: APIModels.SessionInfo) -> some View {
        HStack(alignment: .center, spacing: V.Space.m) {
            Image(systemName: "rectangle.connected.to.line.below")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(V.textSecondary)
                .frame(width: 20)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(s.surface ?? "Unknown device")
                    .font(V.Font.cardTitle)
                    .foregroundStyle(V.text)
                HStack(spacing: V.Space.s) {
                    if let ref = s.session_ref, !ref.isEmpty {
                        Text(ref)
                            .font(V.Font.mono)
                            .foregroundStyle(V.textTertiary)
                            .textSelection(.enabled)
                    }
                    if let expires = s.expires_in {
                        Text(expiry(expires))
                            .font(V.Font.caption)
                            .foregroundStyle(V.textSecondary)
                    }
                }
            }

            Spacer(minLength: V.Space.s)

            if let ref = s.session_ref, !ref.isEmpty {
                Button("Revoke") { revokeTarget = s }
                    .buttonStyle(VQuietButtonStyle(tint: V.danger))
                    .help("Sign \(s.surface ?? "this device") out now")
            } else {
                Text("Cannot revoke")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textTertiary)
                    .help("This session has no reference to revoke")
            }
        }
    }

    private func expiry(_ seconds: Int) -> String {
        if seconds <= 0 { return "expired" }
        let hours = seconds / 3600
        if hours >= 1 { return "expires in \(hours)h" }
        return "expires in \(max(seconds / 60, 1))m"
    }

    private func loadSessions() async {
        isLoading = true
        loadError = nil
        do {
            sessions = try await api.get("/auth/sessions", as: [APIModels.SessionInfo].self)
        } catch {
            // R3.3: a failed load is not the same as "no active sessions" — showing the
            // empty message would read as "you're safe, no sessions" when the list simply
            // couldn't be fetched.
            loadError = "Couldn't load active sessions."
        }
        isLoading = false
    }

    private func revoke(_ ref: String) async {
        do {
            let _: [String: String] = try await api.delete("/auth/sessions/\(ref)")
            await loadSessions()
        } catch {
            // R3.3: a failed revoke must NOT read as success. The previous `catch {}`
            // left the UI as if the session were revoked when it may still be active —
            // a safety problem, not just a cosmetic one.
            revokeError = "Revoke failed: \(error.localizedDescription)"
        }
    }
}

// MARK: - Privacy

struct PrivacySettings: View {
    @State private var privacy: [String: AnyCodable]?
    @State private var isLoading = true
    @State private var confirmDeleteAll = false
    @State private var isDeleting = false
    @State private var isExporting = false
    @State private var banner: String?
    @State private var deleteError: String?
    @State private var exportError: String?
    @State private var loadError: String?
    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.section) {
            PageHeader(
                title: "Privacy",
                subtitle: "What Vantage keeps on this Mac, and how to take it with you or remove it."
            )

            if let banner {
                Notice(kind: .success, message: banner, actionTitle: "Dismiss") {
                    self.banner = nil
                }
            }
            if let exportError {
                Notice(kind: .error, message: exportError, actionTitle: "Dismiss") {
                    self.exportError = nil
                }
            }
            if let deleteError {
                Notice(kind: .error, message: deleteError, actionTitle: "Dismiss") {
                    self.deleteError = nil
                }
            }

            SectionBlock(title: "Data management") {
                dataCard
            }

            SectionBlock(title: "Take or remove") {
                actionsCard
            }
        }
        .task { await load() }
        .vConfirm(
            "Delete ALL data?",
            message: "This removes memories, conversations, audit history, projects, tasks, and every stored credential on this laptop. This cannot be undone.",
            confirm: "Delete Everything",
            isPresented: $confirmDeleteAll
        ) {
            Task { await deleteAllData() }
        }
    }

    // MARK: Data management

    private var dataCard: some View {
        Card {
            if isLoading {
                LoadingRow(text: "Reading privacy settings…")
            } else if let loadError {
                Notice(kind: .error, message: loadError, actionTitle: "Retry") {
                    Task { await load() }
                }
            } else if let privacy {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    ForEach(Array(privacy.keys.sorted().enumerated()), id: \.element) { index, key in
                        if index > 0 { Divider().overlay(V.divider) }
                        evidenceRow(key: key, value: privacy[key]?.value)
                    }
                }
            } else {
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func evidenceRow(key: String, value: Any?) -> some View {
        let label = key.replacingOccurrences(of: "_", with: " ").capitalized
        if let flag = value as? Bool {
            HStack(spacing: V.Space.s) {
                Text(label)
                    .font(V.Font.label)
                    .foregroundStyle(V.textSecondary)
                    .frame(width: 104, alignment: .leading)
                StatusPill(kind: flag ? .connected : .idle, text: flag ? "On" : "Off")
                Spacer(minLength: 0)
            }
        } else {
            DetailRow(label: label, value: display(value))
        }
    }

    /// Renders whatever the backend reported without guessing at its shape:
    /// booleans were previously printed as `Optional(true)` in some paths, and
    /// a list as a raw Swift array literal.
    private func display(_ value: Any?) -> String {
        switch value {
        case let string as String:  return string
        case let bool as Bool:      return bool ? "On" : "Off"
        case let int as Int:        return "\(int)"
        case let double as Double:  return String(format: "%.2f", double)
        case let list as [Any]:     return list.map { "\($0)" }.joined(separator: ", ")
        case let dict as [String: Any]: return "\(dict.count) entries"
        default:                    return "—"
        }
    }

    // MARK: Actions

    private var actionsCard: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.m) {
                HStack(alignment: .top, spacing: V.Space.m) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Export data")
                            .font(V.Font.cardTitle)
                            .foregroundStyle(V.text)
                        Text("Fetches everything Vantage stores and writes it to a JSON file you choose. Redacted unless the backend is asked otherwise.")
                            .font(V.Font.body)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: V.Space.m)
                    Button(isExporting ? "Exporting…" : "Export…") {
                        Task { await exportData() }
                    }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(isExporting || isDeleting)
                    .help("Choose where to save the export")
                }

                Divider().overlay(V.divider)

                HStack(alignment: .top, spacing: V.Space.m) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Delete all data")
                            .font(V.Font.cardTitle)
                            .foregroundStyle(V.danger)
                        Text("Removes memories, conversations, audit history, projects, tasks, and every stored credential on this laptop. This cannot be undone.")
                            .font(V.Font.body)
                            .foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: V.Space.m)
                    Button(isDeleting ? "Deleting…" : "Delete All Data…") {
                        confirmDeleteAll = true
                    }
                    .buttonStyle(VDestructiveButtonStyle())
                    .disabled(isDeleting || isExporting)
                    .help("Delete everything Vantage stores on this Mac")
                }
            }
        }
    }

    private struct DeleteAllBody: Encodable {
        let scope: String
        let confirm: Bool
    }

    /// Fetch the export and actually save it.
    ///
    /// The old control read `/privacy/export` and threw the response away —
    /// "Export Data" did nothing a person could observe. A save panel plus a
    /// success notice with the real path makes the action honest; a failed
    /// export is surfaced rather than swallowed.
    @MainActor
    private func exportData() async {
        let panel = NSSavePanel()
        panel.title = "Export Vantage data"
        panel.nameFieldStringValue = "vantage-export.json"
        panel.allowedContentTypes = [.json]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }

        isExporting = true
        exportError = nil
        defer { isExporting = false }
        do {
            let data: Data = try await api.getRaw("/privacy/export")
            try data.write(to: url, options: .atomic)
            banner = "Exported to \(url.path)"
        } catch {
            // R3.3: a failed export must be surfaced, never silently
            // swallowed — the previous `catch {}` read as "exported".
            exportError = "Export failed: \(error.localizedDescription)"
        }
    }

    @MainActor
    private func deleteAllData() async {
        isDeleting = true
        deleteError = nil
        defer { isDeleting = false }
        do {
            let _: [String: String] = try await api.post(
                "/privacy/delete",
                body: DeleteAllBody(scope: "everything", confirm: true)
            )
            banner = "All data was deleted."
            await load()
        } catch {
            deleteError = "The delete was not completed: \(error.localizedDescription)"
        }
    }

    private func load() async {
        isLoading = true
        loadError = nil
        do {
            privacy = try await api.get("/privacy", as: [String: AnyCodable].self)
        } catch {
            // R3.3: a failed load renders nothing before (nil privacy + no branch), which
            // reads as an empty/incomplete Data Management section.
            loadError = "Couldn't load data-management settings."
        }
        isLoading = false
    }
}

// MARK: - About

struct AboutSettings: View {
    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.section) {
            PageHeader(title: "About")

            SectionBlock(title: "Vantage") {
                Card {
                    VStack(alignment: .leading, spacing: V.Space.l) {
                        HStack(spacing: V.Space.m) {
                            ZStack {
                                Circle()
                                    .strokeBorder(V.goldBorder, lineWidth: 1.5)
                                    .frame(width: 36, height: 36)
                                Image(systemName: "sparkle")
                                    .font(.system(size: 15, weight: .semibold))
                                    .foregroundStyle(V.accent)
                            }
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Vantage")
                                    .font(.system(size: 17, weight: .semibold))
                                    .foregroundStyle(V.text)
                                Text("A permission-controlled personal AI assistant, built on the Jarvis Forge engine.")
                                    .font(V.Font.body)
                                    .foregroundStyle(V.textSecondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }

                        Divider().overlay(V.divider)

                        DetailRow(label: "Version", value: appVersion)
                        DetailRow(label: "Platform", value: "Native macOS")
                        DetailRow(label: "Engine", value: "Jarvis Forge, running on this Mac")
                    }
                }
            }

            Text("© 2025–2026 Vantage")
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
        }
    }

    /// Read from the bundle so the number shown can never drift from
    /// Info.plist again (a hardcoded "0.2.0" would survive every release).
    /// A bare `swift run` has no Info.plist, so the dev build labels itself.
    private var appVersion: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
    }
}