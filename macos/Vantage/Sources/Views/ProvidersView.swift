import SwiftUI

/// AI Provider key management using existing backend endpoints.
struct ProvidersView: View {

    @State private var providers: [APIModels.ProviderInfo] = []
    @State private var activeProviderId: String?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var showAddSheet = false
    @State private var testResult: (provider: String, success: Bool, message: String)?
    @State private var showTestResult = false

    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // One column for the whole screen: header, banner, cards and the
            // selection panel share a width, so "Add Key" sits above the cards
            // it acts on instead of far to their right.
            PageHeader(title: "AI Providers",
                       subtitle: "Keys and models for the AI that answers you.") {
                Button(action: { showAddSheet = true }) {
                    Label("Add Key", systemImage: "plus")
                }
                .buttonStyle(VPrimaryButtonStyle())
            }
            .padding(.horizontal, V.Space.gutter)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.top, V.Space.section)
            .padding(.bottom, V.Space.l)

            if let error = errorMessage {
                ErrorBanner(message: error, retry: { Task { await loadProviders() } })
                    .padding(.horizontal, V.Space.gutter)
                    .frame(maxWidth: 760)
                    .frame(maxWidth: .infinity, alignment: .center)
            }

            if isLoading {
                Spacer()
                ProgressView().frame(maxWidth: .infinity)
                Spacer()
            } else {
                providerList
            }

            // Selection info
            if !providers.isEmpty {
                Divider().overlay(V.divider)
                    .padding(.horizontal, V.Space.gutter)
                    .frame(maxWidth: 760)
                    .frame(maxWidth: .infinity, alignment: .center)
                SelectionInfoView()
                    .padding(V.Space.section)
                    .frame(maxWidth: 760, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .navigationTitle("AI Providers")
        .task { await loadProviders() }
        .refreshable { await loadProviders() }
        .sheet(isPresented: $showAddSheet) {
            AddProviderSheet(isPresented: $showAddSheet) { await loadProviders() }
        }
        .alert(testResult?.success == true ? "Test Passed" : "Test Failed",
               isPresented: $showTestResult,
               presenting: testResult) { _ in
            Button("OK") {}
        } message: { result in
            Text(result.message)
        }
    }

    private var providerList: some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                // With no keys stored and no error, this used to be an entirely
                // blank panel under an "Add Key" button -- the owner could not
                // tell a fresh install from a screen that failed to draw.
                if providers.isEmpty {
                    VStack(spacing: 10) {
                        Image(systemName: errorMessage == nil
                              ? "key.slash" : "questionmark.circle")
                            .font(.largeTitle)
                            .foregroundStyle(errorMessage == nil
                                             ? V.textSecondary : V.warning)
                        Text(errorMessage == nil
                             ? "No provider keys are stored yet."
                             : "I could not read the provider list, so this is not an empty list.")
                            .font(.callout)
                            .foregroundStyle(V.textSecondary)
                            .multilineTextAlignment(.center)
                        if errorMessage == nil {
                            Text("Add a key above, or start the keyless\nchat2api route from the selector.")
                                .font(.caption)
                                .foregroundStyle(V.textTertiary)
                                .multilineTextAlignment(.center)
                        }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 60)
                    .padding(.horizontal, 24)
                }
                ForEach(providers) { provider in
                    ProviderCard(
                        provider: provider,
                        isActive: provider.provider_id == activeProviderId,
                        onTest: { await testProvider(provider.provider_id) },
                        onActivate: { await setActive(provider.provider_id) },
                        onRemove: { await removeKey(provider.provider_id) }
                    )
                }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 16)
            .frame(maxWidth: 700)
            .frame(maxWidth: .infinity, alignment: .center)
        }
    }

    private func loadProviders() async {
        isLoading = true
        errorMessage = nil
        do {
            providers = try await api.get("/ai/providers", as: [APIModels.ProviderInfo].self)
            if let sel: APIModels.ProviderSelection = try? await api.get("/ai/selection") {
                activeProviderId = sel.ai_provider
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func setActive(_ providerId: String) async {
        do {
            let _: APIModels.ProviderSelection = try await api.post(
                "/ai/selection", body: ["ai_provider": providerId]
            )
            activeProviderId = providerId
            await loadProviders()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func testProvider(_ providerId: String) async {
        do {
            let result: APIModels.TestProviderResponse = try await api.post("/ai/providers/\(providerId)/test")
            testResult = (providerId, result.success, result.message ?? "No message returned")
            showTestResult = true
            await loadProviders() // refresh status
        } catch {
            testResult = (providerId, false, error.localizedDescription)
            showTestResult = true
        }
    }

    private func removeKey(_ providerId: String) async {
        do {
            let _: APIModels.DeleteCredentialsResponse = try await api.delete("/ai/providers/\(providerId)/credentials")
            await loadProviders()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - Provider card

struct ProviderCard: View {
    let provider: APIModels.ProviderInfo
    let isActive: Bool
    var onTest: () async -> Void
    var onActivate: () async -> Void
    var onRemove: () async -> Void

    @State private var isTesting = false
    @State private var showRemoveConfirm = false
    @State private var health: APIModels.ProviderHealth?
    private let api = APIClient.shared

    var body: some View {
        HStack(spacing: 12) {
            // Status dot
            Circle()
                .fill(statusColor)
                .frame(width: 10, height: 10)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(provider.display_name)
                        .font(.callout)
                        .fontWeight(.medium)
                    if isActive {
                        Text("Active")
                            .font(.caption2)
                            .foregroundStyle(V.accent)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            .background(Capsule().fill(V.accent.opacity(0.12)))
                    }
                }
                HStack(spacing: 8) {
                    statusLabel
                    if let model = provider.model, !model.isEmpty {
                        Text(model)
                            .font(.caption2)
                            .foregroundStyle(V.textSecondary)
                            .lineLimit(1)
                    }
                }
                healthDetail
            }

            Spacer()

            if provider.configured {
                if !isActive {
                    Button("Set Active") {
                        Task { await onActivate() }
                    }
                    .buttonStyle(.plain)
                    .font(.caption)
                    .foregroundStyle(V.accent)
                }

                Button(probeLabel) {
                    runProbe()
                }
                .buttonStyle(.plain)
                .font(.caption)
                .foregroundStyle(V.accent)
                .disabled(isTesting)

                if isTesting {
                    ProgressView()
                        .controlSize(.small)
                        .scaleEffect(0.7)
                }

                Button {
                    showRemoveConfirm = true
                } label: {
                    Image(systemName: "trash")
                        .font(.caption)
                        .foregroundStyle(V.textSecondary)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: V.Radius.card).fill(V.fill))
        .task { await loadHealth() }
        .alert("Remove Key?", isPresented: $showRemoveConfirm) {
            Button("Remove", role: .destructive) { Task { await onRemove() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Remove the stored credential for \(provider.display_name)?")
        }
    }

    // MARK: - Manual probe

    /// One explicit, user-triggered probe. `isTesting` disables the button for the
    /// whole run, so repeated clicks never dispatch a second paid completion. The
    /// probe goes through the existing `/test` route (which records a `source=test`
    /// observation in the backend's health ledger), then the card re-reads health —
    /// that read is a cheap GET and never issues another completion.
    private func runProbe() {
        guard !isTesting else { return }
        isTesting = true
        Task {
            await onTest()
            await loadHealth()
            isTesting = false
        }
    }

    private func loadHealth() async {
        guard provider.configured else { return }
        // A new health read replaces the old one; the assignment is atomic on the
        // main actor and `isTesting` serialises probe→refresh, so a stale response
        // cannot land after a newer one here.
        health = try? await api.get("/ai/providers/\(provider.provider_id)/health")
    }

    // MARK: - Labels

    /// The action verb depends on what health evidence exists: a degraded/unhealthy
    /// route invites a retry, an unknown route invites a first probe, and a healthy
    /// route keeps the generic word.
    private var probeLabel: String {
        switch health?.route_health {
        case "unhealthy", "degraded": return "Retry"
        case "unknown": return "Probe Route"
        default: return "Test"
        }
    }

    @ViewBuilder
    private var healthDetail: some View {
        if !provider.configured {
            Text("Not configured — add a key to test")
                .font(.caption2)
                .foregroundStyle(V.textSecondary)
                .lineLimit(1)
        } else if let h = health {
            if h.gateway_reachable == false {
                Text("Gateway unreachable — route health unknown")
                    .font(.caption2)
                    .foregroundStyle(V.danger)
                    .lineLimit(1)
            } else {
                Text(healthSummary(h))
                    .font(.caption2)
                    .foregroundStyle(healthColor(h))
                    .lineLimit(1)
            }
        } else {
            // Health not yet read (first appearance): don't claim anything.
            Text("Checking route…")
                .font(.caption2)
                .foregroundStyle(V.textSecondary)
                .lineLimit(1)
        }
    }

    private func healthSummary(_ h: APIModels.ProviderHealth) -> String {
        var parts: [String] = []
        if let label = h.route_health_label, !label.isEmpty {
            parts.append(label)
        }
        if let up = h.upstream_model, !up.isEmpty {
            parts.append("via \(up)")
        }
        if let cf = h.consecutive_failures, cf > 0 {
            parts.append("(\(cf) failures)")
        }
        return parts.isEmpty ? "Route status unknown" : parts.joined(separator: " · ")
    }

    private func healthColor(_ h: APIModels.ProviderHealth) -> Color {
        if h.gateway_reachable == false { return V.danger }
        switch h.route_health {
        case "healthy": return V.accent
        case "degraded": return V.warning
        case "unhealthy": return V.danger
        case "not_configured": return .secondary
        default: return .secondary.opacity(0.7)
        }
    }

    private var statusColor: Color {
        provider.verified ? V.accent : provider.configured ? V.gold : V.textTertiary.opacity(0.4)
    }

    private var statusLabel: some View {
        Text(statusText)
            .font(.caption2)
            .foregroundStyle(statusColor)
    }

    private var statusText: String {
        if !provider.configured { return "Not configured" }
        switch provider.test_status {
        case "passed": return "Verified"
        case "failed": return "Test failed"
        case "untested", .none: return "Not tested"
        default: return "Not tested"
        }
    }
}

// MARK: - Add provider sheet

struct AddProviderSheet: View {
    @Binding var isPresented: Bool
    var onAdded: () async -> Void

    @State private var selectedProvider = "openai"
    @State private var apiKey = ""
    @State private var baseURL = ""
    @State private var model = ""
    @State private var isSaving = false
    @State private var errorMessage: String?

    private let api = APIClient.shared

    private let providerOptions: [(id: String, name: String)] = [
        ("openai", "OpenAI"),
        ("gemini", "Google Gemini"),
        ("groq", "Groq"),
        ("9router", "9Router"),
        ("openai_compatible", "OpenAI-compatible (custom)"),
        ("chat2api", "Chat2API pool"),
    ]

    private var isCustom: Bool { selectedProvider == "openai_compatible" }

    /// The loopback pool authenticates nobody, so a key would be a field nobody
    /// can fill in. Its endpoint is fixed, so this sheet only adds it.
    private var isKeyless: Bool { selectedProvider == "chat2api" }

    private var isReady: Bool {
        let keyOK = !apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if isKeyless { return true }
        if isCustom {
            // A custom OpenAI-compatible endpoint needs the key AND a base URL
            // (the model is optional — the server may default it, but require
            // a base URL so we never point at an empty host).
            return keyOK && !baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        return keyOK
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Add Provider Key")
                .font(.headline)

            Picker("Provider", selection: $selectedProvider) {
                ForEach(providerOptions, id: \.id) { opt in
                    Text(opt.name).tag(opt.id)
                }
            }
            .pickerStyle(.menu)
            .labelsHidden()

            if isKeyless {
                Text("Local pool at http://127.0.0.1:8099 — no API key required.")
                    .font(.caption)
                    .foregroundStyle(V.textSecondary)
            } else {
                SecureField("API Key", text: $apiKey)
                    .textFieldStyle(.roundedBorder)
            }

            if isCustom {
                TextField("Base URL (e.g. http://127.0.0.1:PORT/v1)", text: $baseURL)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                TextField("Model id (optional — e.g. zcode/gpt-4o)", text: $model)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
            }

            if let error = errorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(V.danger)
            }

            HStack {
                Spacer()
                Button("Cancel") { isPresented = false }
                    .keyboardShortcut(.escape)
                Button("Save") {
                    isSaving = true
                    Task {
                        await saveKey()
                        isSaving = false
                    }
                }
                .keyboardShortcut(.return)
                .disabled(!isReady || isSaving)
            }
        }
        .padding(24)
        .frame(width: 440)
    }

    private func saveKey() async {
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        let url = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let modelId = model.trimmingCharacters(in: .whitespacesAndNewlines)
        if !isKeyless {
            guard !key.isEmpty else { return }
        }
        if isCustom && url.isEmpty {
            errorMessage = "A base URL is required for a custom provider."
            return
        }
        do {
            // The backend /store endpoint reads "api_key" and, for a custom
            // provider, persists llm_base_url/llm_model to *this* provider's own
            // config block (not the global active selection), so the Test button
            // resolves this endpoint without overwriting another provider's.
            var body: [String: String] = [:]
            if !key.isEmpty { body["api_key"] = key }
            if isKeyless {
                // The service's own model id: the pool names it in /v1/models, and
                // the gateway may serve several, so record the one it advertises.
                body["llm_base_url"] = "http://127.0.0.1:8099/v1"
                body["llm_model"] = "gpt-4o-mini"
            }
            if isCustom {
                if !url.isEmpty { body["llm_base_url"] = url }
                if !modelId.isEmpty { body["llm_model"] = modelId }
            }
            let _: APIModels.StoreCredentialsResponse = try await api.post(
                "/ai/providers/\(selectedProvider)/store", body: body
            )
            await onAdded()
            isPresented = false
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - Selection info

struct SelectionInfoView: View {
    @State private var selection: APIModels.ProviderSelection?
    @State private var loadFailed = false
    private let api = APIClient.shared

    private static let providerNames: [String: String] = [
        "openai": "OpenAI",
        "gemini": "Google Gemini",
        "groq": "Groq",
        "9router": "9Router",
        "openai_compatible": "OpenAI-compatible (custom)",
        "chat2api": "Chat2API pool",
    ]

    private var displayName: String {
        guard let id = selection?.ai_provider else { return "Unknown" }
        return Self.providerNames[id] ?? id
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Current Selection")
                .font(.caption)
                .foregroundStyle(V.textSecondary)

            if let sel = selection {
                if let provider = sel.ai_provider, !provider.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(displayName)
                            .font(.callout)
                            .fontWeight(.medium)
                        if let model = sel.llm_model, !model.isEmpty {
                            Text("Model: \(model)")
                                .font(.caption2)
                                .foregroundStyle(V.textSecondary)
                        }
                        if sel.credential_configured == false {
                            Text("No API key stored for this provider")
                                .font(.caption2)
                                .foregroundStyle(V.warning)
                        } else {
                            Text(sel.llm_enabled == true ? "Ready" : "Not enabled")
                                .font(.caption2)
                                .foregroundStyle(sel.llm_enabled == true ? V.accent : V.textTertiary)
                        }
                    }
                } else {
                    Text("No AI provider selected")
                        .font(.callout)
                        .foregroundStyle(V.textSecondary)
                }
            } else if loadFailed {
                HStack(spacing: 6) {
                    Text("Selection unavailable — backend not reachable")
                        .font(.caption)
                        .foregroundStyle(V.danger)
                    // R3.3: the failure state must offer a path back out, not just
                    // sit there. Retry re-runs the fetch.
                    Button("Retry", action: { Task { await loadSelection() } })
                        .buttonStyle(.plain)
                        .font(.caption)
                        .foregroundStyle(V.accent)
                }
            } else {
                Text("Loading…")
                    .font(.callout)
                    .foregroundStyle(V.textSecondary)
            }
        }
        .task {
            await loadSelection()
        }
    }

    private func loadSelection() async {
        loadFailed = false
        do {
            selection = try await api.get("/ai/selection", as: APIModels.ProviderSelection.self)
        } catch {
            loadFailed = true
        }
    }
}