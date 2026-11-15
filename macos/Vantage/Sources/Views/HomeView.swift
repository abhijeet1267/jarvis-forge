import SwiftUI

/// Presentation state of one Home screen section. R3.3: a failed fetch must be a
/// distinct state, never collapsed into "loaded empty" — before this, an erroring
/// `/devices` fetch rendered exactly like a backend with zero devices. Kept as a
/// value (not a view) so the invariant is unit-testable without a SwiftUI runtime.
enum SectionPresentation: Equatable {
    case empty          // fetched cleanly; there is genuinely nothing
    case hasItems       // fetched cleanly; data is present
    case failed(String) // the fetch failed — never rendered as empty
}

/// Maps a section's fetch outcome + item count to its presentation. A non-nil
/// `error` means the fetch failed (a distinct, non-empty state); otherwise the
/// count decides between a genuine empty result and populated data.
func sectionPresentation(error: String?, itemCount: Int) -> SectionPresentation {
    if let error { return .failed(error) }
    return itemCount > 0 ? .hasItems : .empty
}

/// Operational overview showing backend status, provider availability,
/// pending approvals, connected devices, active tasks.
struct HomeView: View {

    /// Observe the shared manager — do NOT own it with @StateObject (see ContentView:
    /// a view-owned @StateObject releases the shared singleton when the view goes away,
    /// which cancelled the backend's health polling).
    @ObservedObject private var backend = BackendManager.shared
    @State private var providers: [APIModels.ProviderInfo] = []
    @State private var devices: [APIModels.Device] = []
    @State private var tasks: [APIModels.TaskInfo] = []
    @State private var pendingCount: Int = 0
    /// Everything that needs a person right now (`/attention`).
    @State private var attention: [APIModels.AttentionItem] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    /// Per-section fetch failures (R3.3). Keyed by section ("providers",
    /// "devices", "tasks", "pending"). A non-nil entry means that section's
    /// fetch FAILED — it must be shown as an error, never as an empty list.
    @State private var sectionErrors: [String: String] = [:]

    private let api = APIClient.shared

    /// A greeting that matches the actual time of day. Small, but the Home
    /// screen is the first thing a person sees and a fixed "Home" title tells
    /// them nothing.
    private var greeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<12: return "Good morning"
        case 12..<18: return "Good afternoon"
        default: return "Good evening"
        }
    }

    /// The launch pad: every tile navigates to a screen that exists and does
    /// real work. These are the actions a person actually starts from, so they
    /// come before any status metric.
    private var launchPad: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: V.Space.s)],
                      spacing: V.Space.s) {
                ForEach(QuickAction.all) { action in
                    Button {
                        AppNavigation.shared.go(action.destination)
                    } label: {
                        VStack(alignment: .leading, spacing: V.Space.xs) {
                            Image(systemName: action.symbol)
                                .font(.system(size: 16, weight: .light))
                                .foregroundStyle(V.accent)
                            Text(action.title)
                                .font(V.Font.label)
                                .foregroundStyle(V.text)
                            Text(action.detail)
                                .font(V.Font.caption)
                                .foregroundStyle(V.textTertiary)
                                .lineLimit(2)
                                .multilineTextAlignment(.leading)
                        }
                        .frame(maxWidth: .infinity, minHeight: 78, alignment: .leading)
                        .padding(V.Space.m)
                        .background(V.surface)
                        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
                        .overlay(RoundedRectangle(cornerRadius: V.Radius.card)
                            .stroke(V.border, lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(action.title). \(action.detail)")
                }
            }
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: greeting, subtitle: "What would you like Vantage to do?")

                launchPad

                if let error = errorMessage {
                    ErrorBanner(message: error) { Task { await loadAll() } }
                }

                // Backend status row
                StatusCard(
                    title: "Backend",
                    icon: "network",
                    status: backendStatus,
                    detail: backendDetail
                )

                // Needs attention — the first thing a person should see is a
                // problem, not a metric. Shown only when there are items; a
                // failed fetch is a section error, never "all clear".
                if !attention.isEmpty {
                    SectionHeader("Needs attention")
                    Card(tone: .warning) {
                        VStack(alignment: .leading, spacing: V.Space.s) {
                            ForEach(attention) { item in
                                HStack(alignment: .top, spacing: V.Space.s) {
                                    Image(systemName: "exclamationmark.triangle.fill")
                                        .foregroundStyle(V.warning)
                                        .font(.system(size: 11, weight: .semibold))
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(item.title).font(V.Font.body)
                                            .foregroundStyle(V.text)
                                        if let d = item.detail, !d.isEmpty {
                                            Text(d).font(V.Font.caption)
                                                .foregroundStyle(V.textSecondary)
                                        }
                                    }
                                    Spacer()
                                }
                            }
                        }
                    }
                } else if let err = sectionErrors["attention"], !backendDown {
                    Notice(kind: .error, message: err, actionTitle: "Retry") {
                        Task { await loadAll() }
                    }
                }

                // The single row that replaces every per-section error bar when
                // nothing at all could be read.
                if backendDown {
                    Notice(kind: .error,
                           message: "Could not read " + unreadSections.joined(separator: ", ")
                            + " — the backend did not answer.",
                           actionTitle: "Retry") {
                        Task { await loadAll() }
                    }
                }

                // Quick stats grid
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                    MetricCard(
                        title: "Pending Approvals",
                        value: valueOrUnknown("pending", "\(pendingCount)"),
                        icon: "checkmark.shield.fill",
                        color: pendingCount > 0 ? V.gold : V.accent
                    )
                    MetricCard(
                        title: "Devices",
                        value: valueOrUnknown("devices", "\(devices.count)"),
                        icon: "macbook.and.iphone",
                        color: V.textSecondary
                    )
                    MetricCard(
                        title: "Active Tasks",
                        value: valueOrUnknown("tasks", "\(tasks.filter { $0.status == "running" }.count)"),
                        icon: "clock.arrow.circlepath",
                        color: V.textSecondary
                    )
                    MetricCard(
                        title: "AI Providers",
                        value: valueOrUnknown("providers", "\(providers.filter { $0.configured }.count)/\(providers.count)"),
                        icon: "key.fill",
                        color: providers.contains(where: { $0.verified }) ? V.accent : V.textTertiary
                    )
                }

                // R3.3: context + recovery for the metric-only sections (which have no
                // section below to hold their error row). Shows only when one failed.
                if !failedMetricOnlySections.isEmpty && !backendDown {
                    Notice(kind: .error,
                           message: failedMetricOnlySections.map { $0.0 }.joined(separator: ", ")
                            + " could not be loaded.",
                           actionTitle: "Retry") {
                        Task { await loadAll() }
                    }
                }

                // Provider status — rendered by presentation, so a failed fetch
                // shows an error + retry, never an empty-looking section (R3.3).
                if case .hasItems = sectionPresentation(error: sectionErrors["providers"], itemCount: providers.count) {
                    SectionHeader("AI Providers")
                    ForEach(providers) { p in
                        ProviderStatusRow(provider: p)
                    }
                } else if case .failed(let msg) = sectionPresentation(error: sectionErrors["providers"], itemCount: providers.count), !backendDown {
                    SectionErrorRow(message: msg) { Task { await loadAll() } }
                }

                // Connected devices
                if case .hasItems = sectionPresentation(error: sectionErrors["devices"], itemCount: devices.count) {
                    SectionHeader("Connected Devices")
                    ForEach(devices) { d in
                        DeviceStatusRow(device: d)
                    }
                } else if case .failed(let msg) = sectionPresentation(error: sectionErrors["devices"], itemCount: devices.count), !backendDown {
                    SectionErrorRow(message: msg) { Task { await loadAll() } }
                }
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("Home")
        .task { await loadAll() }
        .refreshable { await loadAll() }
    }

    // MARK: - Computed

    /// R3.3: "Active Tasks" and "Pending Approvals" are *metric-only* — they have no
    /// section below to carry their own error row, so a failed fetch would otherwise show
    /// only a bare "—" in the stats grid with no explanation or way to retry. This lists
    /// those failed sections so the "—" always has a reason and a recovery action. The
    /// list sections (providers/devices) are excluded: they already render their own
    /// SectionErrorRow below, and repeating them here would double the message.
    /// True when every section read failed, which in practice means the backend
    /// is unreachable rather than five separate problems. Measured on this
    /// screen with the server down: five identical orange Retry bars, then the
    /// same fact stated again. One row that names what could not be read is the
    /// honest shape of "the backend is down".
    private var backendDown: Bool {
        let all = ["attention", "providers", "devices", "tasks", "pending"]
        return all.allSatisfy { sectionErrors[$0] != nil }
    }

    /// The sections a collapsed row has to name.
    private var unreadSections: [String] {
        self.sectionErrors.keys.sorted().map { key in
            switch key {
            case "attention": return "attention"
            case "providers": return "AI providers"
            case "devices": return "devices"
            case "tasks": return "tasks"
            case "pending": return "pending approvals"
            default: return key
            }
        }
    }

    private var failedMetricOnlySections: [(name: String, message: String)] {
        let metricOnly = ["pending": "Pending Approvals", "tasks": "Active Tasks"]
        return metricOnly.compactMap { key, label in
            guard let message = sectionErrors[key] else { return nil }
            return (label, message)
        }.sorted { $0.0 < $1.0 }
    }

    /// R3.3: a metric whose section failed to load must not show a confident "0"
    /// (that would read as "there are none"). Unknown is shown instead, and the
    /// detailed section below carries the error + retry.
    private func valueOrUnknown(_ section: String, _ knownValue: String) -> String {
        sectionErrors[section] == nil ? knownValue : "—"
    }

    private var backendStatus: StatusCard.Status {
        switch backend.state {
        case .running: return .ok
        case .starting, .checking: return .warn
        case .error, .stopped: return .bad
        case .unknown: return .off
        }
    }

    private var backendDetail: String {
        if let h = backend.health {
            var parts: [String] = ["\(h.tools ?? 0) tools"]
            if let src = h.llm_source { parts.append("LLM: \(src)") }
            if let prov = h.ai_provider { parts.append("Provider: \(prov)") }
            return parts.joined(separator: " · ")
        }
        // health is nil: the state tells the truth, not a bare "Loading…" that
        // would sit next to a red dot and read as stuck.
        switch backend.state {
        case .checking, .starting, .unknown: return "Connecting…"
        case .error(let message): return message.isEmpty ? "Backend unreachable" : message
        case .stopped(let reason): return "Stopped — \(reason)"
        case .running: return "Connecting…"
        }
    }

    // MARK: - Data loading

    private func loadAll() async {
        isLoading = true
        errorMessage = nil
        sectionErrors = [:]   // R3.3: a previous failure must not persist into a fresh load
        await backend.refreshHealth()
        async let provs: () = loadProviders()
        async let devs: () = loadDevices()
        async let tsk: () = loadTasks()
        async let pend: () = loadPending()
        async let att: () = loadAttention()
        _ = await (provs, devs, tsk, pend, att)
        isLoading = false
    }

    private func loadAttention() async {
        do {
            attention = try await api.attention()
            sectionErrors["attention"] = nil
        } catch {
            // A failed read is NOT "nothing wrong"; say the check could not run.
            sectionErrors["attention"] = "Couldn't check what needs attention."
        }
    }

    private func loadProviders() async {
        do {
            providers = try await api.get("/ai/providers", as: [APIModels.ProviderInfo].self)
            sectionErrors["providers"] = nil
        } catch {
            // R3.3: providers is optional on Home, so this is never page-fatal —
            // but it is NOT the same as "no providers"; record the failure so the
            // section can say it couldn't load instead of vanishing.
            sectionErrors["providers"] = "Couldn't load providers."
        }
    }

    private func loadDevices() async {
        do {
            devices = try await api.get("/devices", as: [APIModels.Device].self)
            sectionErrors["devices"] = nil
        } catch {
            sectionErrors["devices"] = "Couldn't load devices."
        }
    }

    private func loadTasks() async {
        do {
            let raw: [APIModels.TaskInfo] = try await api.get("/tasks")
            tasks = raw
            sectionErrors["tasks"] = nil
        } catch {
            sectionErrors["tasks"] = "Couldn't load tasks."
        }
    }

    private func loadPending() async {
        do {
            let raw: [APIModels.Confirmation] = try await api.get("/confirmations")
            pendingCount = raw.count
            sectionErrors["pending"] = nil
        } catch {
            sectionErrors["pending"] = "Couldn't load pending approvals."
        }
    }
}

// MARK: - Subviews

struct StatusCard: View {
    enum Status { case ok, warn, bad, off }

    let title: String
    let icon: String
    let status: Status
    let detail: String

    var body: some View {
        Card {
            HStack(spacing: V.Space.m) {
                Image(systemName: icon)
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(statusColor)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(V.Font.cardTitle)
                        .foregroundStyle(V.text)
                    Text(detail)
                        .font(V.Font.caption)
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                StatusPill(kind: statusKind, text: statusLabel)
            }
        }
    }

    private var statusKind: StatusKind {
        switch status {
        case .ok: return .connected
        case .warn: return .pending
        case .bad: return .error
        case .off: return .idle
        }
    }

    private var statusLabel: String {
        switch status {
        case .ok: return "Running"
        case .warn: return "Starting"
        case .bad: return "Down"
        case .off: return "Unknown"
        }
    }

    private var statusColor: Color { statusKind.colour }
}

struct MetricCard: View {
    let title: String
    let value: String
    let icon: String
    let color: Color

    var body: some View {
        Card(padding: V.Space.m) {
            HStack(spacing: V.Space.s) {
                Image(systemName: icon)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(color)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(value)
                        .font(.system(size: 20, weight: .semibold).monospacedDigit())
                        .foregroundStyle(V.text)
                    Text(title)
                        .font(V.Font.caption)
                        .foregroundStyle(V.textSecondary)
                }
                Spacer()
            }
        }
    }
}

struct SectionHeader: View {
    let _title: String
    init(_ title: String) { self._title = title }

    var body: some View {
        Text(_title.uppercased())
            .font(V.Font.caption.weight(.semibold))
            .tracking(0.6)
            .foregroundStyle(V.textTertiary)
            .padding(.top, V.Space.xs)
    }
}

struct ProviderStatusRow: View {
    let provider: APIModels.ProviderInfo

    var body: some View {
        HStack(spacing: V.Space.s) {
            Circle()
                .fill(provider.verified ? V.accent
                      : provider.configured ? V.gold : V.textTertiary.opacity(0.4))
                .frame(width: 8, height: 8)
            Text(provider.display_name)
                .font(V.Font.body)
                .foregroundStyle(V.text)
            Spacer()
            if provider.configured {
                StatusPill(kind: .connected, text: "Configured")
            }
        }
        .padding(.vertical, V.Space.xxs + 1)
    }
}

struct DeviceStatusRow: View {
    let device: APIModels.Device

    var body: some View {
        HStack(spacing: V.Space.s) {
            Text(device.displayName)
                .font(V.Font.body)
                .foregroundStyle(V.text)
            Spacer()
            if device.is_host == true {
                Text("This Mac")
                    .font(V.Font.caption)
                    .foregroundStyle(V.textTertiary)
            }
            // The dot follows the reported liveness, not a constant green: an
            // offline device drawn as online is the same lie as an empty list
            // for a failed read.
            Circle()
                .fill(device.online == true ? V.accent : V.textTertiary.opacity(0.4))
                .frame(width: 6, height: 6)
        }
        .padding(.vertical, V.Space.xxs + 1)
    }
}

/// Section-level error (R3.3): shown where a section's fetch failed, so a
/// failure is never indistinguishable from a genuinely empty section. Carries a
/// retry, reusing the app's existing orange-error visual language.
struct SectionErrorRow: View {
    let message: String
    var retry: (() -> Void)?

    var body: some View {
        Notice(kind: .error, message: message, actionTitle: retry == nil ? nil : "Retry",
               action: retry)
    }
}

/// Reusable error banner with retry button.
struct ErrorBanner: View {
    let message: String
    var retry: (() -> Void)?

    var body: some View {
        Notice(kind: .error, message: message, actionTitle: retry == nil ? nil : "Retry",
               action: retry)
    }
}

/// The Home launch pad's actions. Each one is a real screen that exists in the
/// sidebar, so a tile can never lead somewhere that does nothing.
private struct QuickAction: Identifiable {
    let id: String
    let title: String
    let detail: String
    let symbol: String
    let destination: NavItem

    static let all: [QuickAction] = [
        QuickAction(id: "command", title: "Command Center",
                    detail: "Ask for something in your own words",
                    symbol: "bubble.left.and.bubble.right.fill", destination: .command),
        QuickAction(id: "missions", title: "Mission Control",
                    detail: "What Vantage is working on, and what it proved",
                    symbol: "target", destination: .missions),
        QuickAction(id: "smartfiles", title: "Smart Files",
                    detail: "Find duplicates and see what is taking space",
                    symbol: "folder.badge.gearshape", destination: .smartfiles),
        QuickAction(id: "files", title: "Preview file changes",
                    detail: "Inspect and approve edits before they happen",
                    symbol: "rectangle.split.3x1", destination: .parallelworld),
        QuickAction(id: "skills", title: "Skills",
                    detail: "Reusable automations and how they have performed",
                    symbol: "square.stack.3d.up", destination: .skills),
        QuickAction(id: "doctor", title: "Project Doctor",
                    detail: "Find out what is actually wrong with a project",
                    symbol: "stethoscope", destination: .doctor),
        QuickAction(id: "goals", title: "GoalGuard",
                    detail: "Watch a service or file, and why it last failed",
                    symbol: "eye", destination: .goals),
        QuickAction(id: "safety", title: "Safety Center",
                    detail: "What Vantage may do right now, and how to stop it",
                    symbol: "lock.shield", destination: .safety),
    ]
}
