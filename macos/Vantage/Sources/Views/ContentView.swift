import SwiftUI

/// Root navigation: a quiet sidebar, a page surface, and one place that decides
/// the app's appearance.
///
/// Three changes from the earlier shell, each with a reason:
///
/// 1. **Light-first.** `VantageApp` used to pin `.preferredColorScheme(.dark)`,
///    so the warm-white design could never appear. Appearance is now decided
///    once, and dark still works from the same token set.
/// 2. **A real selected state.** The stock sidebar selection was a system blue
///    bar that fought the emerald/gold palette. Rows draw their own selection.
/// 3. **Screens ease in.** A short crossfade on navigation; skipped entirely
///    when Reduce Motion is on.
struct ContentView: View {

    @ObservedObject private var backend = BackendManager.shared
    @ObservedObject private var flows = FlowStore.shared
    @ObservedObject private var engine = FlowRuntime.shared.engine
    @ObservedObject private var control = MacControlCoordinator.shared
    @State private var selection: NavItem = .home
    /// A request the global palette handed to the full window. Held here so the
    /// Command Center opens on the right text instead of the palette quietly
    /// answering something it should not.
    @State private var handedOff: HandedOff?
    /// Cross-feature navigation (palette → inbox/briefing/documents, a capture
    /// item → a flow draft, a briefing card → a flow's history). Typed payloads,
    /// so a handoff cannot be re-parsed into something different.
    @ObservedObject private var navigation = AppNavigation.shared
    /// A flow draft handed to the Flows screen. Cleared once consumed.
    @State private var flowDraftHandoff: Flow?

    var body: some View {
        HStack(spacing: 0) {
            VantageSidebar(selection: $selection, backend: backend)
                .frame(width: 208)

            Rectangle()
                .fill(V.divider)
                .frame(width: 1)
                .ignoresSafeArea()

            detailPane
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(V.background)
        }
        .background(V.background)
        .sheet(item: Binding(get: { control.pendingForPalette.map(PaletteApproval.init) },
                             set: { if $0 == nil { control.cancelPalettePending() } })) { item in
            PaletteApprovalSheet(summary: item.summary) {
                Task { await control.confirmPalettePending() }
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .anywhereAskConversation)) { note in
            let text = (note.userInfo?["text"] as? String)
                ?? (note.userInfo?["prefill"] as? String) ?? ""
            if !text.isEmpty {
                handedOff = HandedOff(text: text)
                selection = .command
            } else {
                selection = .command
            }
        }
        .onChange(of: engine.pendingApproval) { pending in
            // A flow the palette started paused on a protected step: show the
            // person exactly that step in the screen that can approve it.
            if pending != nil { selection = .flows }
        }
        .onReceive(navigation.$request) { request in
            guard let request else { return }
            // A flow drafted elsewhere is handed to Flows as an *unsaved* draft.
            // Every other handoff is navigation plus an optional prefill.
            if let draft = request.flowDraft { flowDraftHandoff = draft }
            if request.captureID != nil { CaptureInboxView.pendingFocus = request.captureID }
            if let flowID = request.flowID { FlowsView.pendingFocus = flowID }
            if let prefill = request.prefill { AnywhereController.shared.pendingPrefill = prefill }
            V.animate { selection = request.destination }
            navigation.consume()
        }
        .task {
            FlowRuntime.shared.attach { selection = .flows }
            await backend.connect()
            // The footer can only say "Older backend — upgrade available" if
            // the script's verdict has been read. Status is a read-only
            // question, so asking at launch is safe and changes nothing.
            await backend.refreshLifecycle()
        }
        // No .onDisappear { disconnect() }: this app is a service monitor.
        // Teardown happens once, on quit, in VantageApp.applicationWillTerminate.
    }

    @ViewBuilder
    private var detailPane: some View {
        // `.id` restarts a screen's own `.task` when you navigate back to it, so a
        // screen holding a stale error refreshes instead of keeping it.
        Group {
            switch selection {
            case .today:        TodayView()
            case .home:         HomeView()
            case .missions:     MissionControlView()
            case .smartfiles:   SmartFilesView()
            case .parallelworld: ParallelWorldView()
            case .safety:       SafetyCenterView()
            case .skills:       SkillsLibraryView()
            case .goals:        GoalGuardView()
            case .doctor:       ProjectDoctorView()
            case .planner:      PlannerView()
            case .operator_:    OperatorView()
            case .control:      MacControlView()
            case .workspaces:   WorkspacesView()
            case .flows:        FlowsView(handoff: $flowDraftHandoff)
            case .work:         WorkView()
            case .people:       PeopleView()
            case .connections:  ConnectionsView()
            case .google:       GoogleView()
            case .mcp:          MCPView()
            case .automation:   N8NView()
            case .command:      CommandCenterView(handoff: $handedOff)
            case .providers:    ProviderTabsView()
            case .capabilities: CapabilityRoutingView()
            case .devices:      DevicesView()
            case .approvals:    ApprovalsView()
            case .activity:     ActivityView()
            case .memory:       MemoryView()
            case .settings:     SettingsView()
            case .briefing:     BriefingView()
            case .capture:      CaptureInboxView()
            case .documents:    DocumentsView()
            case .preferences:  PreferencesView()
            }
        }
        .id(selection)
        .transition(.opacity)
        .animation(V.motionAllowed ? V.quick : nil, value: selection)
    }
}

// MARK: - Sidebar

private struct VantageSidebar: View {
    @Binding var selection: NavItem
    @ObservedObject var backend: BackendManager

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            brand

            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    group("Workspace", [.today, .home, .briefing, .planner, .workspaces, .command])
                    group("Capture & Documents", [.capture, .documents])
                    group("Connections", [.connections, .google, .mcp, .automation])
                    group("Mac", [.control, .flows, .operator_])
                    group("People & Work", [.work, .people])
                    group("Configure", [.providers, .capabilities, .devices, .preferences, .settings])
                    group("Oversight", [.approvals, .activity, .memory])
                }
                .padding(.horizontal, V.Space.s)
                .padding(.vertical, V.Space.m)
            }

            Spacer(minLength: 0)
            StatusFooter(backend: backend)
        }
        .background(V.sidebar)
    }

    private var brand: some View {
        HStack(spacing: V.Space.s) {
            // A restrained mark: one gold ring, one emerald glyph. No gradient.
            ZStack {
                Circle()
                    .strokeBorder(V.goldBorder, lineWidth: 1.5)
                    .frame(width: 26, height: 26)
                Image(systemName: "sparkle")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(V.accent)
            }
            VStack(alignment: .leading, spacing: 0) {
                Text("Vantage")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(V.text)
                Text("personal AI")
                    .font(.system(size: 10))
                    .foregroundStyle(V.textTertiary)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, V.Space.l)
        .padding(.top, V.Space.l)
        .padding(.bottom, V.Space.s)
    }

    private func group(_ title: String, _ items: [NavItem]) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title.uppercased())
                .font(.system(size: 9.5, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(V.textTertiary)
                .padding(.horizontal, V.Space.s + 2)
                .padding(.bottom, V.Space.xs)
            ForEach(items, id: \.self) { item in
                SidebarRow(item: item, isSelected: selection == item) {
                    if selection != item {
                        V.animate { selection = item }
                    }
                }
            }
        }
    }
}

private struct SidebarRow: View {
    let item: NavItem
    let isSelected: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: V.Space.s) {
                Image(systemName: item.symbol)
                    .font(.system(size: 12.5, weight: .medium))
                    .frame(width: 17)
                    .foregroundStyle(isSelected ? V.accent : V.textSecondary)
                Text(item.title)
                    .font(.system(size: 12.5, weight: isSelected ? .semibold : .regular))
                    .foregroundStyle(isSelected ? V.text : V.textSecondary)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, V.Space.s + 2)
            .padding(.vertical, 5.5)
            .background(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .fill(isSelected ? V.accentSoft : (hovering ? V.fill : .clear))
            )
            .overlay(alignment: .leading) {
                // An emerald spine marks selection without a heavy fill.
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(V.accent)
                    .frame(width: 2.5, height: 14)
                    .opacity(isSelected ? 1 : 0)
                    .offset(x: -1)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(item.title)
        .accessibilityLabel(item.title)
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
        .animation(V.motionAllowed ? V.quick : nil, value: hovering)
        .animation(V.motionAllowed ? V.quick : nil, value: isSelected)
    }
}

private struct StatusFooter: View {
    @ObservedObject var backend: BackendManager

    /// Set when the person asks for the recovery the footer offers, so the
    /// confirmation is anchored to the same control that named the problem.
    @State private var confirmingRecovery = false

    private var isLegacy: Bool {
        backend.lifecycleState?.kind == .legacy
    }

    /// The recovery this state permits, or nil when nothing is offered. Mirrors
    /// the Settings rule: only a provably-ours stale backend, or a proven-legacy
    /// one, or a free port may be acted on. A foreign owner, another checkout or
    /// an unknown state shows none — those need a person, not a button.
    private var recoveryTitle: String? {
        guard let status = backend.lifecycleState else { return nil }
        switch status.kind {
        case .ours where status.stale == true: return "Restart Backend…"
        case .legacy where status.migratable: return "Upgrade Backend…"
        case .free: return "Start Backend"
        default: return nil
        }
    }

    private var kind: StatusKind {
        // An older Vantage backend from this checkout is serving traffic but
        // predates /health/identity: usable, but an upgrade is available, so
        // amber "pending" rather than the red of a dead backend.
        if isLegacy { return .pending }
        // A stale backend is connected but running older code than is on
        // disk. It is usable, so the dot is amber (pending) — but the text
        // must not read plain "Backend connected", which is how a stale
        // process went unnoticed.
        if backend.stale && backend.state == .running { return .pending }
        switch backend.state {
        case .running: return .connected
        case .starting, .checking, .unknown: return .pending
        case .error: return .error
        case .stopped: return .idle
        }
    }

    private var text: String {
        if isLegacy { return "Older backend — upgrade available" }
        if backend.stale && backend.state == .running { return "Connected — restart needed" }
        switch backend.state {
        case .unknown, .checking: return "Checking…"
        case .running: return "Backend connected"
        case .starting: return "Starting…"
        case .stopped(let reason): return reason.isEmpty ? "Disconnected" : reason
        case .error(let message): return message.isEmpty ? "Error" : message
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            Rectangle().fill(V.divider).frame(height: 1)
            HStack(spacing: V.Space.s) {
                Image(systemName: kind.symbol)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(kind.colour)
                Text(text)
                    .font(V.Font.caption)
                    .foregroundStyle(V.textSecondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            // The recovery gets its own row: the sidebar is 208pt, and sharing
            // one row with the status truncated the sentence this control
            // exists to explain ("Conn…eeded") and squeezed the button into a
            // wrapped two-line stub.
            if let title = recoveryTitle {
                Button(title) {
                    if backend.lifecycleState?.kind == .free {
                        // Starting nothing is a no-op: no confirmation.
                        Task { await backend.migrateOrRestart() }
                    } else {
                        confirmingRecovery = true
                    }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .controlSize(.small)
                .frame(maxWidth: .infinity, alignment: .leading)
                .disabled(backend.isLifecycleBusy)
                .help("Uses backend_lifecycle.py, which acts only on a backend it can prove is this checkout's.")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, V.Space.l)
        .padding(.vertical, V.Space.s + 2)
        .background(V.sidebar)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Backend status: \(text)")
        .vConfirm(
            recoveryTitle ?? "",
            message: "Vantage stops the backend it can prove is this checkout's, waits for the "
                + "port to close, then starts the current build and checks its identity. "
                + "Nothing else on the machine is signalled.",
            confirm: "Continue",
            isPresented: $confirmingRecovery,
            destructive: false
        ) {
            Task { await backend.migrateOrRestart() }
        }
    }
}

// MARK: - Navigation model

enum NavItem: String, Identifiable, CaseIterable {
    case today, home, missions, smartfiles, parallelworld, safety, skills, goals, doctor, planner, operator_, control, flows, work, people, connections, workspaces,
         google, mcp, automation, command, providers, capabilities, devices, approvals,
         activity, memory, settings
    // Phase 3
    case briefing, capture, documents, preferences

    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: return "Today"
        case .home: return "Home"
        case .missions: return "Mission Control"
        case .smartfiles: return "Smart Files"
        case .parallelworld: return "ParallelWorld"
        case .safety: return "Safety Center"
        case .skills: return "Skills Library"
        case .goals: return "GoalGuard"
        case .doctor: return "Project Doctor"
        case .planner: return "Planner"
        case .operator_: return "Operator"
        case .control: return "Control Mac"
        case .workspaces: return "Workspaces"
        case .flows: return "Flows"
        case .work: return "Work"
        case .people: return "People"
        case .connections: return "Connections"
        case .google: return "Google"
        case .mcp: return "MCP"
        case .automation: return "Automation"
        case .command: return "Command Center"
        case .providers: return "AI Providers"
        case .capabilities: return "Capabilities"
        case .devices: return "Devices"
        case .approvals: return "Approvals"
        case .activity: return "Activity"
        case .memory: return "Memory"
        case .settings: return "Settings"
        case .briefing: return "Daily Briefing"
        case .capture: return "Quick Capture"
        case .documents: return "Documents"
        case .preferences: return "Preferences"
        }
    }

    var symbol: String {
        switch self {
        case .today: return "sun.horizon.fill"
        case .home: return "house.fill"
        case .missions: return "target"
        case .smartfiles: return "folder.badge.gearshape"
        case .parallelworld: return "rectangle.split.3x1"
        case .safety: return "lock.shield"
        case .skills: return "square.stack.3d.up"
        case .goals: return "eye"
        case .doctor: return "stethoscope"
        case .planner: return "calendar.day.timeline.left"
        case .operator_: return "rectangle.on.rectangle"
        case .control: return "slider.horizontal.3"
        case .workspaces: return "square.grid.2x2.fill"
        case .flows: return "square.stack.3d.up"
        case .work: return "hammer.fill"
        case .people: return "person.2.fill"
        case .connections: return "link"
        case .google: return "calendar.badge.clock"
        case .mcp: return "puzzlepiece.extension.fill"
        case .automation: return "point.3.connected.trianglepath.dotted"
        case .command: return "bubble.left.and.bubble.right.fill"
        case .providers: return "key.fill"
        case .capabilities: return "sparkles.rectangle.stack"
        case .devices: return "macbook.and.iphone"
        case .approvals: return "checkmark.shield.fill"
        case .activity: return "clock.arrow.circlepath"
        case .memory: return "brain.head.profile"
        case .settings: return "gearshape.fill"
        case .briefing: return "sun.max.fill"
        case .capture: return "tray.and.arrow.down.fill"
        case .documents: return "doc.text.fill"
        case .preferences: return "slider.horizontal.2.square"
        }
    }
}

// MARK: - Handoffs from Vantage Anywhere

/// Text the palette passed to the full window to answer conversationally.
struct HandedOff: Identifiable, Equatable {
    let id = UUID()
    let text: String
}

/// Identity wrapper so `.sheet(item:)` can present the palette's parked approval.
struct PaletteApproval: Identifiable {
    let id = UUID()
    let summary: String
    init(_ pending: MacControlCoordinator.PendingConfirmation) {
        summary = pending.summary
    }
}

/// The confirmation a palette-originated protected action needs.
///
/// It runs through the same policy as Control Mac: the request shown is the
/// request that runs, and declining leaves everything untouched.
struct PaletteApprovalSheet: View {
    let summary: String
    let onConfirm: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            Text("Vantage Anywhere needs your approval")
                .font(V.Font.cardTitle).foregroundStyle(V.text)
            Text(summary)
                .font(V.Font.body).foregroundStyle(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Text("This changes something on your Mac. Nothing has run yet.")
                .font(V.Font.caption).foregroundStyle(V.textTertiary)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .buttonStyle(VSecondaryButtonStyle())
                Button("Run it") { onConfirm(); dismiss() }
                    .buttonStyle(VPrimaryButtonStyle())
            }
        }
        .padding(V.Space.xl)
        .frame(width: 420)
        .background(V.background)
    }
}
