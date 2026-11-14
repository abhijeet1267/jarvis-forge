import SwiftUI

/// The provider settings screen: **seven tabs**, each with its own provider,
/// model, base URL, key and test.
///
/// The previous version of this screen had one form plus a routing list, so
/// changing the image model visibly changed the chat model. The fix is
/// structural rather than cosmetic:
///
/// * every tab holds its **own drafts** (`drafts[category]`), so typing in one
///   tab cannot alter another;
/// * every save writes **one** category (`PUT /ai/provider-categories/{c}`),
///   and the backend only touches that block;
/// * every test performs **that tab's real operation** — a chat completion, a
///   short synthesis, a tiny image, a one-pixel vision question — so a green
///   pill means the job worked, not that a key exists.
///
/// The provider list and the default base URLs come from the backend
/// catalogue, so a provider that cannot do a job is not offered for it.
struct ProviderTabsView: View {

    /// One tab's editable state, kept whole per category.
    private struct Draft: Equatable {
        var provider: String = ""
        var model: String = ""
        var baseURL: String = ""
        var enabled: Bool = true
        var timeout: Double = 60
        var apiKey: String = ""
        var options: [String: String] = [:]

        init(from settings: APIModels.CategorySettings) {
            provider = settings.provider ?? ""
            model = settings.model ?? ""
            baseURL = settings.base_url ?? ""
            enabled = settings.enabled ?? true
            timeout = settings.timeout ?? 60
            options = settings.options ?? [:]
        }

        init() {}
    }

    @State private var tabs: [APIModels.CategoryTab] = []
    @State private var selected: String =
        ProcessInfo.processInfo.environment["VANTAGE_PROVIDER_TAB"] ?? "text"
    @State private var drafts: [String: Draft] = [:]
    @State private var isLoading = true
    @State private var errorMessage: String?

    @State private var savingCategory: String?
    @State private var savedNote: [String: String] = [:]
    @State private var testingCategory: String?
    @State private var testNotes: [String: String] = [:]
    @State private var testFailures: [String: String] = [:]
    @State private var discoveringCategory: String?
    @State private var discovered: [String: [APIModels.DiscoveredModel]] = [:]

    private let api = APIClient.shared

    private static let titleFor: [String: String] = [
        "text": "Text & Chat",
        "documents": "Document Reading",
        "speech": "Speech",
        "image": "Image",
        "vision": "Vision",
        "video": "Video",
        "custom": "Custom OpenAI-Compatible",
    ]

    private static let symbolFor: [String: String] = [
        "text": "bubble.left.and.text.bubble.right",
        "documents": "doc.text",
        "speech": "waveform",
        "image": "photo",
        "vision": "eye",
        "video": "film",
        "custom": "server.rack",
    ]

    private var current: APIModels.CategoryTab? {
        tabs.first { $0.category == selected }
    }

    private var draft: Draft {
        drafts[selected] ?? Draft()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            PageHeader(title: "AI Providers",
                       subtitle: "One provider per job. Each tab is configured and "
                        + "tested on its own.") {
                if isLoading { ProgressView().controlSize(.small) }
                Button {
                    Task { await load() }
                } label: { Label("Reload", systemImage: "arrow.clockwise") }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(isLoading)
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.top, V.Space.section)
            .padding(.bottom, V.Space.s)

            tabBar

            Divider().overlay(V.divider)

            if let errorMessage {
                Notice(kind: .error, message: errorMessage, actionTitle: "Retry") {
                    Task { await load() }
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.top, V.Space.s)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: V.Space.l) {
                    if let tab = current {
                        form(for: tab)
                    } else if !isLoading {
                        EmptyState(symbol: "key",
                                   title: "No categories",
                                   message: "The backend did not return the provider "
                                    + "categories. Reload, or check that the backend "
                                    + "is running.")
                    }
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.vertical, V.Space.l)
                .frame(maxWidth: 820)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background(V.background)
        .navigationTitle("AI Providers")
        .task { await load() }
        .refreshable { await load() }
    }

    // MARK: - Tabs

    private var tabBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: V.Space.xs) {
                ForEach(tabs) { tab in
                    tabButton(tab)
                }
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.s)
        }
    }

    private func tabButton(_ tab: APIModels.CategoryTab) -> some View {
        let isSelected = tab.category == selected
        let configured = tab.settings.configured == true
        let ok = tab.settings.test_ok == true
        return Button {
            selected = tab.category
        } label: {
            HStack(spacing: 6) {
                Image(systemName: Self.symbolFor[tab.category] ?? "gear")
                    .font(.system(size: 11, weight: .medium))
                Text(Self.titleFor[tab.category] ?? tab.category)
                    .font(V.Font.label)
                // A dot only where there is something true to say: a completed
                // successful test (green) or a configuration that failed its
                // last test (orange). Nothing means "not tested".
                if configured {
                    Circle()
                        .fill(ok ? V.accent : (tab.settings.test_ok == false ? V.warning
                                               : V.textTertiary.opacity(0.4)))
                        .frame(width: 6, height: 6)
                }
            }
            .foregroundStyle(isSelected ? V.text : V.textSecondary)
            .padding(.horizontal, V.Space.m)
            .padding(.vertical, 7)
            .background(isSelected ? V.surface
                        : (configured ? V.fill : Color.clear),
                        in: RoundedRectangle(cornerRadius: V.Radius.control,
                                             style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                .strokeBorder(isSelected ? V.border : Color.clear, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help(tab.purpose)
        .accessibilityLabel("\(Self.titleFor[tab.category] ?? tab.category) tab")
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }

    // MARK: - Form

    private func form(for tab: APIModels.CategoryTab) -> some View {
        let d = drafts[tab.category] ?? Draft(from: tab.settings)
        return VStack(alignment: .leading, spacing: V.Space.m) {
            Card {
                VStack(alignment: .leading, spacing: V.Space.s) {
                    HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                        Image(systemName: Self.symbolFor[tab.category] ?? "gear")
                            .font(.system(size: 15, weight: .medium))
                            .foregroundStyle(V.accent)
                        Text(Self.titleFor[tab.category] ?? tab.category)
                            .font(V.Font.cardTitle).foregroundStyle(V.text)
                        Spacer()
                        statusPill(tab)
                    }
                    Text(tab.purpose)
                        .font(V.Font.body).foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if !tab.capabilities.isEmpty {
                        Text("Capability: " + tab.capabilities.joined(separator: " · "))
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    }
                }
            }

            Card {
                VStack(alignment: .leading, spacing: V.Space.m) {
                    HStack(spacing: V.Space.s) {
                        Text("Provider").font(V.Font.label)
                            .foregroundStyle(V.textSecondary)
                            .frame(width: 92, alignment: .leading)
                        Picker("", selection: providerBinding(tab.category)) {
                            Text("Not set").tag("")
                            ForEach(tab.providers) { provider in
                                Text(provider.label).tag(provider.id)
                            }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 280)
                        Toggle("Enabled", isOn: enabledBinding(tab.category))
                            .toggleStyle(.switch)
                            .controlSize(.mini)
                            .tint(V.accent)
                        Spacer()
                    }

                    fieldRow("Model", placeholder: "model id", tab: tab,
                             text: modelBinding(tab.category))

                    fieldRow("Base URL", placeholder: "https://host/v1", tab: tab,
                             text: baseURLBinding(tab.category), mono: true)

                    HStack(spacing: V.Space.s) {
                        Text("API key").font(V.Font.label)
                            .foregroundStyle(V.textSecondary)
                            .frame(width: 92, alignment: .leading)
                        SecureField(tab.settings.has_key == true
                                    ? "•••••• stored — leave blank to keep"
                                    : "paste the key",
                                    text: keyBinding(tab.category))
                            .textFieldStyle(.plain)
                            .font(V.Font.mono)
                            .padding(.horizontal, V.Space.s)
                            .padding(.vertical, 6)
                            .background(V.surfaceAlt, in: RoundedRectangle(
                                cornerRadius: V.Radius.control, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: V.Radius.control,
                                                      style: .continuous)
                                .strokeBorder(V.border, lineWidth: 1))
                        if tab.settings.has_key == true {
                            StatusPill(kind: .connected, text: "Stored")
                        }
                    }

                    HStack(spacing: V.Space.s) {
                        Text("Timeout").font(V.Font.label)
                            .foregroundStyle(V.textSecondary)
                            .frame(width: 92, alignment: .leading)
                        Stepper(value: timeoutBinding(tab.category), in: 5...600, step: 5) {
                            Text("\(Int(d.timeout)) s").font(V.Font.body)
                        }
                        .frame(maxWidth: 200)
                        Spacer()
                    }

                    if tab.category == "speech" {
                        speechOptions(tab)
                    }
                    if tab.category == "image" {
                        fieldRow("Size", placeholder: "512x512", tab: tab,
                                 text: optionBinding(tab.category, "size"))
                    }
                    if tab.category == "custom" || tab.category == "text" {
                        noticeForCustom(tab)
                    }

                    HStack(spacing: V.Space.s) {
                        Button {
                            Task { await save(tab.category) }
                        } label: {
                            if savingCategory == tab.category {
                                HStack(spacing: V.Space.xs) {
                                    ProgressView().controlSize(.small)
                                    Text("Saving…")
                                }
                            } else { Text("Save") }
                        }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(savingCategory != nil
                                  || (drafts[tab.category]?.provider ?? "").isEmpty)

                        Button {
                            Task { await discover(tab.category) }
                        } label: {
                            if discoveringCategory == tab.category {
                                HStack(spacing: V.Space.xs) {
                                    ProgressView().controlSize(.small)
                                    Text("Listing…")
                                }
                            } else { Text("List models") }
                        }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(discoveringCategory != nil
                                  || (drafts[tab.category]?.baseURL ?? "").isEmpty)
                        .help("GET {base URL}/models. A provider that publishes its "
                              + "capabilities narrows the list to usable models.")

                        Button {
                            Task { await test(tab.category) }
                        } label: {
                            if testingCategory == tab.category {
                                HStack(spacing: V.Space.xs) {
                                    ProgressView().controlSize(.small)
                                    Text("Testing…")
                                }
                            } else { Text("Test connection") }
                        }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(testingCategory != nil
                                  || tab.settings.configured != true)
                        .help("Perform this category's real operation, not just a key check.")
                        Spacer()
                    }

                    if let note = savedNote[tab.category] {
                        Notice(kind: .success, message: note, actionTitle: "Dismiss") {
                            savedNote[tab.category] = nil
                        }
                    }
                    if let failure = testFailures[tab.category] {
                        Notice(kind: .error, message: failure, actionTitle: "Dismiss") {
                            testFailures[tab.category] = nil
                        }
                    } else if let note = testNotes[tab.category] {
                        Notice(kind: .success, message: note, actionTitle: "Dismiss") {
                            testNotes[tab.category] = nil
                        }
                    }
                    if let last = tab.settings.tested_at, tab.settings.test_ok != nil {
                        Text("Last test \(relative(last)): "
                             + (tab.settings.test_ok == true ? "passed" : "failed")
                             + (tab.settings.test_note.map { " — \($0)" } ?? ""))
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            if let models = discovered[tab.category], !models.isEmpty {
                discoveredList(models, tab: tab)
            }
        }
    }

    private func fieldRow(_ label: String, placeholder: String,
                          tab: APIModels.CategoryTab,
                          text: Binding<String>, mono: Bool = false) -> some View {
        HStack(spacing: V.Space.s) {
            Text(label).font(V.Font.label)
                .foregroundStyle(V.textSecondary)
                .frame(width: 92, alignment: .leading)
            VField(label: nil, placeholder: placeholder, text: text)
                .font(mono ? V.Font.mono : V.Font.body)
        }
    }

    private func speechOptions(_ tab: APIModels.CategoryTab) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            fieldRow("Voice", placeholder: "e.g. Kore / alloy",
                     tab: tab, text: optionBinding(tab.category, "voice"))
            fieldRow("Format", placeholder: "mp3", tab: tab,
                     text: optionBinding(tab.category, "format"))
            Text("Speech uses the provider's speech endpoint (/audio/speech, or "
                 + "Gemini's own audio response) — never chat completions.")
                .font(V.Font.caption).foregroundStyle(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func noticeForCustom(_ tab: APIModels.CategoryTab) -> some View {
        let message = tab.category == "custom"
            ? "Any OpenAI-compatible server. The base URL must include the version "
              + "path (/v1) and is not rewritten."
            : "Chat is the assistant's own model. A custom endpoint can be chosen here "
              + "or in the Custom tab."
        Text(message)
            .font(V.Font.caption).foregroundStyle(V.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func discoveredList(_ models: [APIModels.DiscoveredModel],
                                tab: APIModels.CategoryTab) -> some View {
        Card(tone: .ivory) {
            VStack(alignment: .leading, spacing: V.Space.xs) {
                Text("\(models.count) model(s) from the provider")
                    .font(V.Font.label).foregroundStyle(V.textSecondary)
                ForEach(models.prefix(40)) { model in
                    Button {
                        var d = drafts[tab.category] ?? Draft(from: tab.settings)
                        d.model = model.id
                        drafts[tab.category] = d
                    } label: {
                        HStack(spacing: V.Space.s) {
                            Text(model.id).font(V.Font.mono)
                                .foregroundStyle(V.text)
                            Spacer()
                            if let outs = model.output_modalities, !outs.isEmpty {
                                Text(outs.joined(separator: ","))
                                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            }
                            if (drafts[tab.category]?.model ?? "") == model.id {
                                Image(systemName: "checkmark")
                                    .font(.system(size: 10, weight: .semibold))
                                    .foregroundStyle(V.accent)
                            }
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help(model.description ?? model.id)
                }
            }
        }
    }

    private func statusPill(_ tab: APIModels.CategoryTab) -> some View {
        if tab.settings.configured != true {
            return StatusPill(kind: .idle, text: "Not configured")
        }
        if tab.settings.test_ok == true {
            return StatusPill(kind: .connected, text: "Tested")
        }
        if tab.settings.test_ok == false {
            return StatusPill(kind: .error, text: "Test failed")
        }
        return StatusPill(kind: .pending, text: "Configured — untested")
    }

    private func relative(_ timestamp: Double) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        let date = Date(timeIntervalSince1970: timestamp)
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    // MARK: - Bindings (per category, so nothing leaks across tabs)

    private func withDraft(_ category: String,
                           _ change: (inout Draft) -> Void) {
        var d = drafts[category]
            ?? Draft(from: tabs.first { $0.category == category }?.settings
                     ?? APIModels.CategorySettings(category: category, provider: nil,
                                                   provider_label: nil, model: nil,
                                                   base_url: nil, enabled: nil,
                                                   timeout: nil, capability: nil,
                                                   options: nil, has_key: nil,
                                                   configured: nil, tested_at: nil,
                                                   test_ok: nil, test_note: nil))
        change(&d)
        drafts[category] = d
    }

    private func providerBinding(_ category: String) -> Binding<String> {
        let tab = tabs.first { $0.category == category }
        return Binding(
            get: { drafts[category]?.provider ?? tab?.settings.provider ?? "" },
            set: { value in
                withDraft(category) { d in
                    d.provider = value
                    // Fill the provider's known URL only when the field is empty,
                    // so a person's own URL is never overwritten.
                    if d.baseURL.isEmpty,
                       let known = tab?.providers.first(where: { $0.id == value })?
                        .default_base_url, !known.isEmpty {
                        d.baseURL = known
                    }
                }
            })
    }

    private func modelBinding(_ category: String) -> Binding<String> {
        let tab = tabs.first { $0.category == category }
        return Binding(
            get: { drafts[category]?.model ?? tab?.settings.model ?? "" },
            set: { value in withDraft(category) { $0.model = value } })
    }

    private func baseURLBinding(_ category: String) -> Binding<String> {
        let tab = tabs.first { $0.category == category }
        return Binding(
            get: { drafts[category]?.baseURL ?? tab?.settings.base_url ?? "" },
            set: { value in withDraft(category) { $0.baseURL = value } })
    }

    private func keyBinding(_ category: String) -> Binding<String> {
        Binding(
            get: { drafts[category]?.apiKey ?? "" },
            set: { value in withDraft(category) { $0.apiKey = value } })
    }

    private func enabledBinding(_ category: String) -> Binding<Bool> {
        let tab = tabs.first { $0.category == category }
        return Binding(
            get: { drafts[category]?.enabled ?? tab?.settings.enabled ?? true },
            set: { value in withDraft(category) { $0.enabled = value } })
    }

    private func timeoutBinding(_ category: String) -> Binding<Double> {
        let tab = tabs.first { $0.category == category }
        return Binding(
            get: { drafts[category]?.timeout ?? tab?.settings.timeout ?? 60 },
            set: { value in withDraft(category) { $0.timeout = value } })
    }

    private func optionBinding(_ category: String, _ key: String) -> Binding<String> {
        let tab = tabs.first { $0.category == category }
        return Binding(
            get: { drafts[category]?.options[key] ?? tab?.settings.options?[key] ?? "" },
            set: { value in withDraft(category) { $0.options[key] = value } })
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let payload = try await api.providerCategories()
            tabs = payload.categories ?? []
            // Reset drafts from the saved settings on a fresh load, so the form
            // shows what is stored rather than a stale local edit.
            drafts = [:]
            for tab in tabs {
                drafts[tab.category] = Draft(from: tab.settings)
            }
            if !tabs.contains(where: { $0.category == selected }),
               let first = tabs.first {
                selected = first.category
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func save(_ category: String) async {
        guard savingCategory == nil else { return }
        let d = drafts[category] ?? Draft()
        savingCategory = category
        savedNote[category] = nil
        defer { savingCategory = nil }
        do {
            _ = try await api.saveProviderCategory(
                category, provider: d.provider, model: d.model, baseURL: d.baseURL,
                enabled: d.enabled, timeout: d.timeout, options: d.options,
                apiKey: d.apiKey.isEmpty ? nil : d.apiKey)
            savedNote[category] = "Saved. Other tabs were not changed."
            // Re-read so the stored state (including has_key) is what shows.
            let payload = try await api.providerCategories()
            tabs = payload.categories ?? []
            var fresh = drafts[category] ?? d
            fresh.apiKey = ""
            drafts[category] = fresh
        } catch {
            testFailures[category] = error.localizedDescription
        }
    }

    private func test(_ category: String) async {
        guard testingCategory == nil else { return }
        testingCategory = category
        testNotes[category] = nil
        testFailures[category] = nil
        defer { testingCategory = nil }
        do {
            let result = try await api.testProviderCategory(category)
            if let fresh = result.category { apply(fresh) }
            if result.ok == true {
                testNotes[category] = result.detail ?? "The provider performed the operation."
            } else {
                testFailures[category] = result.error ?? "The test did not succeed."
            }
            let payload = try await api.providerCategories()
            tabs = payload.categories ?? []
        } catch {
            testFailures[category] = error.localizedDescription
        }
    }

    private func discover(_ category: String) async {
        guard discoveringCategory == nil else { return }
        discoveringCategory = category
        defer { discoveringCategory = nil }
        do {
            let found = try await api.discoverProviderModels(category)
            if found.ok != true {
                testFailures[category] = found.error ?? "The provider did not list models."
                return
            }
            // Prefer the models that can do this tab's job when the provider
            // publishes capabilities.
            let models = (found.matching?.isEmpty == false ? found.matching
                          : found.models) ?? []
            discovered[category] = models
            if models.isEmpty {
                testFailures[category] = "The provider listed no models."
            }
        } catch {
            testFailures[category] = error.localizedDescription
        }
    }

    private func apply(_ settings: APIModels.CategorySettings) {
        guard let index = tabs.firstIndex(where: { $0.category == settings.category })
        else { return }
        tabs[index] = APIModels.CategoryTab(category: tabs[index].category,
                                            purpose: tabs[index].purpose,
                                            capabilities: tabs[index].capabilities,
                                            providers: tabs[index].providers,
                                            settings: settings)
    }
}