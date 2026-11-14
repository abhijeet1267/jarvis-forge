import SwiftUI

/// The in-app AI settings screen: point Vantage at any OpenAI-compatible provider,
/// store a key securely, and pick a model fetched from the provider itself.
@available(iOS 16.0, macOS 13.0, *)
struct VantageAISettingsView: View {

    @StateObject private var settings = AISettingsManager.shared

    // The key is entered into local `@State` and only written to the Keychain on
    // Save; it is never bound directly to a stored property, so an unsaved edit
    // cannot leak into persistent storage.
    @State private var keyDraft: String = ""
    @State private var revealKey = false

    @State private var models: [String] = []
    @State private var fetchState: FetchState = .idle
    @State private var saveMessage: String?

    private enum FetchState: Equatable {
        case idle
        case loading
        case loaded(Int)
        case failed(String)
    }

    /// Quick presets so the common providers are one tap away. All are
    /// OpenAI-compatible; the user may still type any other base URL.
    private static let presets: [(String, String)] = [
        ("OpenAI", "https://api.openai.com/v1"),
        ("OpenRouter", "https://openrouter.ai/api/v1"),
        ("Groq", "https://api.groq.com/openai/v1"),
        ("DeepSeek", "https://api.deepseek.com/v1"),
        ("Ollama (local)", "http://localhost:11434/v1"),
        ("LM Studio (local)", "http://localhost:1234/v1"),
    ]

    var body: some View {
        Form {
            providerSection
            keySection
            modelsSection
            assistantSection
            siriSection
        }
        .navigationTitle("Vantage AI")
        .task { if models.isEmpty { await fetchModels() } }
    }

    // MARK: - Sections

    private var providerSection: some View {
        Section {
            TextField("Base URL", text: $settings.baseURL)
                .autocorrectionDisabled()
                #if os(iOS)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                #endif

            ScrollView(.horizontal, showsIndicators: false) {
                HStack {
                    ForEach(Self.presets, id: \.0) { name, url in
                        Button(name) { settings.baseURL = url }
                            .font(.caption)
                            .buttonStyle(.bordered)
                    }
                }
            }
        } header: {
            Text("Provider")
        } footer: {
            Text("Any OpenAI-compatible endpoint works (OpenAI, OpenRouter, Groq, DeepSeek, Ollama, LM Studio).")
        }
    }

    private var keySection: some View {
        Section {
            HStack {
                if revealKey {
                    TextField("API Key", text: $keyDraft)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                } else {
                    SecureField("API Key", text: $keyDraft)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                }
                Button {
                    revealKey.toggle()
                } label: {
                    Image(systemName: revealKey ? "eye.slash" : "eye")
                }
                .buttonStyle(.borderless)
            }

            HStack {
                Button("Save Key") { saveKey() }
                    .disabled(keyDraft.trimmingCharacters(in: .whitespaces).isEmpty)
                Spacer()
                if settings.hasAPIKey {
                    Button("Remove", role: .destructive) { removeKey() }
                }
            }

            Label(
                settings.hasAPIKey ? "A key is stored in the Keychain" : "No key stored",
                systemImage: settings.hasAPIKey ? "checkmark.seal" : "exclamationmark.triangle"
            )
            .font(.caption)
            .foregroundStyle(settings.hasAPIKey ? .green : .secondary)

            if let saveMessage {
                Text(saveMessage).font(.caption).foregroundStyle(.red)
            }
        } header: {
            Text("API Key")
        } footer: {
            Text("Stored in the iOS Keychain on this device. It is never written to preferences or logs.")
        }
    }

    private var modelsSection: some View {
        Section {
            Button {
                Task { await fetchModels() }
            } label: {
                HStack {
                    Text("Fetch Models")
                    if fetchState == .loading { Spacer(); ProgressView() }
                }
            }
            .disabled(fetchState == .loading)

            switch fetchState {
            case .failed(let message):
                Text(message).font(.caption).foregroundStyle(.red)
            case .loaded(let count) where count == 0:
                Text("The provider returned no models.").font(.caption).foregroundStyle(.secondary)
            default:
                EmptyView()
            }

            if models.isEmpty {
                Text("No models loaded yet. Set a base URL and key, then Fetch Models.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Picker("Model", selection: $settings.selectedModel) {
                    Text("Select a model").tag("")
                    ForEach(models, id: \.self) { Text($0).tag($0) }
                }
            }
        } header: {
            Text("Model")
        } footer: {
            Text("Models are fetched from \(settings.baseURL)/models using your key.")
        }
    }

    private var assistantSection: some View {
        Section {
            TextField("System prompt", text: $settings.systemPrompt, axis: .vertical)
                .lineLimit(2...4)
            VStack(alignment: .leading) {
                Text("Temperature: \(settings.temperature, specifier: "%.1f")")
                    .font(.caption)
                Slider(value: $settings.temperature, in: 0...1, step: 0.1)
            }
        } header: {
            Text("Assistant")
        }
    }

    private var siriSection: some View {
        Section {
            Label("“Hey Siri, ask Vantage a question”", systemImage: "waveform")
            Link("Set up a Siri shortcut",
                 destination: URL(string: "shortcuts://")!)
        } header: {
            Text("Siri")
        } footer: {
            Text("The Siri intent answers with your configured provider. Add it in the Shortcuts app, or ask Siri directly.")
        }
    }

    // MARK: - Actions

    private func saveKey() {
        saveMessage = nil
        if settings.setAPIKey(keyDraft) {
            keyDraft = ""
        } else {
            saveMessage = "The Keychain refused the key; it was not saved."
        }
    }

    private func removeKey() {
        _ = settings.deleteAPIKey()
        models = []
        fetchState = .idle
    }

    private func fetchModels() async {
        fetchState = .loading
        do {
            let client = try AIClient.from(settings)
            let fetched = try await client.fetchModels()
            models = fetched.map(\.id).sorted()
            fetchState = .loaded(models.count)
            // Keep a still-valid selection; otherwise fall back to the first model.
            if !models.contains(settings.selectedModel) {
                settings.selectedModel = models.first ?? ""
            }
        } catch {
            models = []
            fetchState = .failed((error as? LocalizedError)?.errorDescription
                                 ?? "Could not fetch models.")
        }
    }
}