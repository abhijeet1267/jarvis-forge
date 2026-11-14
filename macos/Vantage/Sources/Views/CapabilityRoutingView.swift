import SwiftUI

/// Per-task model assignment for the capabilities the text assistant does not
/// cover: speech, images, vision, video.
///
/// The distinction this screen exists to make visible is **declared vs
/// verified**:
///
/// * *Declared* — a provider and model were chosen. Nothing has proven they
///   work for this capability.
/// * *Verified* — a real, bounded request for this exact capability succeeded
///   for this exact model. Only a Test does that, and only within the last
///   week; an older success is shown as expired rather than kept forever.
///
/// A text model answering once is not evidence that it can speak, draw or see.
/// This screen never presents one as if it were.
struct CapabilityRoutingView: View {

    @State private var routing: APIModels.CapabilityRouting?
    @State private var providers: [APIModels.ProviderInfo] = []
    @State private var isLoading = true
    @State private var errorMessage: String?

    /// Per-task editor state, so two rows cannot share a selection.
    @State private var draftProvider: [String: String] = [:]
    @State private var draftModel: [String: String] = [:]
    @State private var specified: [String: Bool] = [:]
    /// The task currently being tested, and its live result.
    @State private var testing: String?
    @State private var testNotes: [String: String] = [:]
    @State private var testFailures: [String: String] = [:]

    private let api = APIClient.shared

    /// The capabilities a person recognises, with what they are for.
    private static let labels: [(task: String, title: String, symbol: String,
                                purpose: String)] = [
        ("text_to_speech", "Speech output", "speaker.wave.2",
         "Reads answers aloud. Needs a text-to-speech model — a chat model is not one."),
        ("speech_to_text", "Speech input", "mic",
         "Turns a recording into text."),
        ("image_generation", "Image generation", "photo.badge.plus",
         "Creates a new image from a description."),
        ("image_editing", "Image editing", "wand.and.stars",
         "Changes an existing image when the provider supports it."),
        ("vision", "Image understanding", "eye",
         "Answers questions about a picture. Needs a model that accepts image input."),
        ("video_generation", "Video generation", "film",
         "Submits a video job and reports its id. A queued job is not a finished video."),
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.section) {
                header
                if let errorMessage {
                    Notice(kind: .error, message: errorMessage, actionTitle: "Retry") {
                        Task { await load() }
                    }
                }
                SectionHeader("Capabilities")
                ForEach(Self.labels, id: \.task) { entry in
                    capabilityCard(entry)
                }
                footnote
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 800)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("Capabilities")
        .task { await load() }
        .refreshable { await load() }
    }

    private var header: some View {
        PageHeader(
            title: "Capabilities",
            subtitle: "Which model does speech, images, vision and video. "
                + "Separate from the chat model on purpose."
        ) {
            if isLoading { ProgressView().controlSize(.small) }
        }
    }

    // MARK: - One capability

    private func capabilityCard(_ entry: (task: String, title: String, symbol: String,
                                          purpose: String)) -> some View {
        let assignment = routing?.assignments?[entry.task]
        return Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: entry.symbol)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(V.textSecondary)
                    Text(entry.title)
                        .font(V.Font.cardTitle).foregroundStyle(V.text)
                    Spacer()
                    statusPill(assignment)
                }
                Text(entry.purpose)
                    .font(V.Font.body)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)

                if let note = assignment?.primary?.note, !note.isEmpty {
                    Text(note)
                        .font(V.Font.caption)
                        .foregroundStyle(assignment?.primary?.isVerified == true
                                         ? V.textTertiary : V.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }

                HStack(spacing: V.Space.s) {
                    Picker("Provider", selection: providerBinding(entry.task)) {
                        Text("Not set").tag("")
                        ForEach(providerIDs, id: \.self) { id in
                            Text(id).tag(id)
                        }
                    }
                    .frame(maxWidth: 200)

                    VField(label: nil, placeholder: "model id",
                           text: modelBinding(entry.task))
                        .frame(maxWidth: 260)

                    Button("Save") { Task { await save(entry.task) } }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled((draftProvider[entry.task] ?? "") == "")

                    Button {
                        Task { await test(entry.task) }
                    } label: {
                        if testing == entry.task {
                            HStack(spacing: V.Space.xs) {
                                ProgressView().controlSize(.small)
                                Text("Testing…")
                            }
                        } else {
                            Text("Test")
                        }
                    }
                    .buttonStyle(VQuietButtonStyle())
                    .disabled(testing != nil || assignment?.configured != true)
                    .help("Send one real, minimal request for this capability. "
                          + "This is the only thing that verifies it.")
                    Spacer()
                }

                if let failure = testFailures[entry.task] {
                    Notice(kind: .error, message: failure, actionTitle: "Dismiss") {
                        testFailures[entry.task] = nil
                    }
                } else if let note = testNotes[entry.task] {
                    Notice(kind: .success, message: note, actionTitle: "Dismiss") {
                        testNotes[entry.task] = nil
                    }
                }
            }
        }
    }

    private var providerIDs: [String] {
        // A provider that can hold a key is what this list is; the picker must
        // not offer something the backend would refuse.
        let ids = providers.map(\.id)
        return ids.isEmpty ? ["openai", "gemini", "groq", "openrouter", "openai_compatible"]
                           : ids
    }

    @ViewBuilder
    private func statusPill(_ assignment: APIModels.CapabilityAssignment?) -> some View {
        if assignment?.configured != true {
            StatusPill(kind: .idle, text: "Not set")
        } else if assignment?.primary?.isVerified == true {
            StatusPill(kind: .connected, text: "Verified")
        } else {
            // Deliberately not green: configured is not proven.
            StatusPill(kind: .pending, text: "Declared")
        }
    }

    private func providerBinding(_ task: String) -> Binding<String> {
        Binding(
            get: { draftProvider[task] ?? routing?.assignments?[task]?.primary?.provider_id ?? "" },
            set: { draftProvider[task] = $0 })
    }

    private func modelBinding(_ task: String) -> Binding<String> {
        Binding(
            get: { draftModel[task] ?? routing?.assignments?[task]?.primary?.model ?? "" },
            set: { draftModel[task] = $0 })
    }

    private var footnote: some View {
        Text("A model is “Declared” until a Test performs the real request for that exact "
             + "capability and model. Verification expires after a week, because a key "
             + "can be revoked or a model withdrawn — an old success is a statement about "
             + "the past. With nothing assigned, callers are told the capability is "
             + "unavailable rather than receiving a placeholder.")
            .font(V.Font.caption)
            .foregroundStyle(V.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            async let routingCall = api.capabilityRouting()
            async let providersCall = api.get("/ai/providers", as: [APIModels.ProviderInfo].self)
            routing = try await routingCall
            providers = (try? await providersCall) ?? []
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func save(_ task: String) async {
        let provider = draftProvider[task] ?? ""
        guard !provider.isEmpty else { return }
        errorMessage = nil
        do {
            _ = try await api.assignCapability(task: task, providerID: provider,
                                               model: draftModel[task] ?? "")
            // Clearing the drafts lets the fresh assignment show through.
            draftProvider[task] = nil
            draftModel[task] = nil
            routing = try await api.capabilityRouting()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func test(_ task: String) async {
        guard testing == nil else { return }
        testing = task
        testNotes[task] = nil
        testFailures[task] = nil
        defer { testing = nil }
        do {
            let result = try await api.testCapability(task: task)
            if let fresh = result.assignments { applyAssignments(fresh) }
            if result.ok == true {
                testNotes[task] = verificationNote(result)
            } else {
                testFailures[task] = result.error ?? "The test did not succeed."
            }
        } catch {
            testFailures[task] = error.localizedDescription
        }
    }

    /// What was actually proven, named precisely — including "bytes of audio"
    /// so a person can tell a real synthesis from an empty answer.
    private func verificationNote(_ result: APIModels.CapabilityTestResult) -> String {
        guard let media = result.result else { return "Verified by a real request." }
        if let bytes = media.bytes, bytes > 0 {
            return "Verified: the provider returned \(bytes) bytes of "
                + "\(media.content_type ?? "data")."
        }
        if let text = media.text, !text.isEmpty {
            return "Verified: the provider answered “\(text.prefix(80))”."
        }
        if let job = media.job_id, !job.isEmpty {
            return "Submitted a job (\(job)). It is not verified until the job finishes — "
                + "a queued job is not a result."
        }
        return media.detail ?? "Verified by a real request."
    }

    private func applyAssignments(_ fresh: [String: APIModels.CapabilityAssignment]) {
        guard let existing = routing else { return }
        routing = APIModels.CapabilityRouting(tasks: existing.tasks,
                                              routed: existing.routed,
                                              assignments: fresh)
    }
}