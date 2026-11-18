import SwiftUI

/// Voice Operator V2 — the optional voice surface.
///
/// This is a *client* of the paths that already exist. It owns no execution:
/// every command goes through `ConversationService.send`, which posts to the
/// backend's `/command`, which runs the planner, the permission engine and the
/// mission runner. Speech in comes from `VoiceService` (the signed app, because
/// macOS attributes microphone and Speech consent to the bundle); speech out
/// uses the same `AVSpeechSynthesizer` every other surface already uses.
///
/// The one thing this screen adds that the Command Center does not is a
/// deliberate *review* step: a transcript is shown, editable, and only sent when
/// the person presses Send. The Command Center submits a finished transcript
/// immediately, which is right for a keyboard-driven screen and wrong for a
/// microphone, where recognition is a guess until a human reads it.
///
/// Nothing here is decorative. Every control either calls a real method or is
/// visibly disabled with the reason it cannot run.
struct VoiceOperatorV2View: View {

    @ObservedObject private var voice = VoiceService.shared
    /// Owned here, not shared with the Command Center.
    ///
    /// The conversation service carries the session id that makes a follow-up
    /// resolve, so two screens sharing one instance would interleave two
    /// people-facing conversations into a single memory. A separate instance
    /// per surface is the correct isolation; it was also required, since the
    /// type has no singleton.
    @StateObject private var chat = ConversationService()
    @ObservedObject private var backend = BackendManager.shared

    /// Preview/demo fixtures, injected only by the offscreen renderer.
    ///
    /// The real app constructs this view with no arguments, so both stay nil and
    /// every branch below reads live service state. The renderer passes fixtures
    /// so a populated conversation and a parked approval card can be *seen*
    /// rather than assumed to lay out correctly. Nothing here can make the app
    /// act: the fixtures are display data, and every button still calls the
    /// real service, which is not running in a render pass.
    private let previewMessages: [ChatMessage]?
    private let previewApproval: PendingDesktopApproval?

    init(previewMessages: [ChatMessage]? = nil,
         previewApproval: PendingDesktopApproval? = nil) {
        self.previewMessages = previewMessages
        self.previewApproval = previewApproval
    }

    /// What the microphone produced, before the person commits to it.
    @State private var draftTranscript = ""
    /// A typed command, for the same path when no microphone is available.
    @State private var typedCommand = ""
    /// Set while a turn is in flight, so the Send button cannot double-submit.
    @State private var isSending = false
    /// Whether the transcript field should be focused after recording stops.
    @FocusState private var transcriptFocused: Bool

    /// What the transcript area shows: fixtures in a render pass, live otherwise.
    private var displayedMessages: [ChatMessage] {
        previewMessages ?? chat.conversation.messages
    }

    /// The parked plan to show: the fixture if one was injected, else the real one.
    private var displayedApproval: PendingDesktopApproval? {
        previewApproval ?? chat.pendingDesktopApproval
    }

    private var composerText: String {
        VoiceOperatorComposer.text(draftTranscript: draftTranscript,
                                   typedCommand: typedCommand)
    }

    private var canSend: Bool {
        VoiceOperatorComposer.canSend(draftTranscript: draftTranscript,
                                      typedCommand: typedCommand,
                                      isSending: isSending,
                                      isThinking: chat.isThinking)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Rectangle().fill(V.divider).frame(height: 1)
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if displayedMessages.isEmpty {
                            emptyState
                        } else {
                            ForEach(displayedMessages) { message in
                                messageRow(message)
                            }
                        }
                        if let approval = displayedApproval {
                            approvalCard(approval)
                        }
                        if let error = chat.lastError, !error.isEmpty {
                            errorRow(error)
                        }
                    }
                    .frame(maxWidth: 760, alignment: .leading)
                    .padding(22)
                    .frame(maxWidth: .infinity)
                }
            }
            Rectangle().fill(V.divider).frame(height: 1)
            composer
        }
        .background(V.background)
        .navigationTitle("Voice Operator")
        .onAppear {
            // The live transcript streams while recording; it lands in the
            // editable field so the person can fix a misheard word before it
            // becomes an action. It is never submitted on our initiative.
            voice.onFinalTranscript = { text in
                draftTranscript = text
                transcriptFocused = true
            }
            Task { await chat.refreshCapability() }
        }
        .onChange(of: voice.liveTranscript) { live in
            // Mirror the in-progress transcript so a long sentence is visible
            // as it is heard, not only at the end.
            if voice.isRecording, !live.isEmpty {
                draftTranscript = live
            }
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Voice Operator")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(V.text)
                Text(subtitle)
                    .font(.system(size: 12))
                    .foregroundStyle(V.textSecondary)
            }
            Spacer()
            if !displayedMessages.isEmpty {
                Button("New session") { chat.startNewConversation(); draftTranscript = "" }
                    .buttonStyle(.plain)
                    .font(.system(size: 12))
                    .foregroundStyle(V.textSecondary)
                    .help("Clear this conversation. The next command starts with no context.")
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 14)
    }

    private var subtitle: String {
        guard chat.capabilityKnown else { return "Checking which AI provider is answering…" }
        if !chat.aiAvailable { return "No AI provider — typed commands and tool actions still run." }
        return chat.providerName.isEmpty
            ? "Speak or type. Every action still goes through the permission gate."
            : "Answers via \(chat.providerName). Actions still go through the permission gate."
    }

    // MARK: - Empty state

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Say what you want done.")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(V.text)
            Text("Press the microphone, read your words back, then send. "
                 + "A command that changes something will still ask for approval "
                 + "before it runs.")
                .font(.system(size: 12))
                .foregroundStyle(V.textSecondary)
            if !voice.microphoneGranted {
                Text("The microphone is not available yet — typing works the same way.")
                    .font(.system(size: 12))
                    .foregroundStyle(V.textSecondary)
            }
        }
        .padding(.vertical, 18)
    }

    // MARK: - Messages

    private func messageRow(_ message: ChatMessage) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(message.role == .user ? "You" : "Vantage")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(V.textSecondary)
            Text(message.text)
                .font(.system(size: 13))
                .foregroundStyle(V.text)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(message.role == .user ? V.surfaceAlt : V.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func errorRow(_ error: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(V.danger)
            Text(error)
                .font(.system(size: 12))
                .foregroundStyle(V.text)
                .textSelection(.enabled)
        }
        .padding(12)
        .background(V.dangerSoft)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    // MARK: - Approval

    /// The parked plan, answered only by a press.
    ///
    /// The backend accepts a supplied confirmation id as the approval, which is
    /// why nothing here is automatic: a screen that answered on its own would be
    /// approving a plan nobody read.
    private func approvalCard(_ approval: PendingDesktopApproval) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Waiting for your approval")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(V.text)
            Text(approval.summary)
                .font(.system(size: 12))
                .foregroundStyle(V.textSecondary)

            ForEach(approval.steps) { step in
                HStack(alignment: .top, spacing: 8) {
                    Text(step.status == "pending" ? "○" : "●")
                        .foregroundStyle(step.status == "pending" ? V.textSecondary : V.success)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(step.title).font(.system(size: 12))
                            .foregroundStyle(V.text)
                        if !step.paramsSummary.isEmpty {
                            Text(step.paramsSummary)
                                .font(.system(size: 11, design: .monospaced))
                                .foregroundStyle(V.textSecondary)
                                .textSelection(.enabled)
                        }
                    }
                }
            }

            HStack(spacing: 8) {
                Button("Approve and run") {
                    Task { await chat.approvePendingDesktop(); await speakLast() }
                }
                .buttonStyle(.borderedProminent)
                .tint(V.accent)

                Button("Deny") {
                    Task { await chat.denyPendingDesktop() }
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(V.accent.opacity(0.4), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    // MARK: - Composer

    private var composer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if voice.isRecording {
                recordingRow
            }

            // The editable transcript. A microphone result is a guess until a
            // person reads it; this field is where that guess becomes a command.
            TextEditor(text: draftTranscript.isEmpty ? $typedCommand : $draftTranscript)
                .focused($transcriptFocused)
                .font(.system(size: 13))
                .frame(minHeight: 54, maxHeight: 110)
                .padding(6)
                .background(V.surface)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(V.divider, lineWidth: 1)
                )
                .overlay(alignment: .topLeading) {
                    if composerText.isEmpty {
                        Text(voice.isRecording ? "Listening…" : "Say or type a command")
                            .font(.system(size: 13))
                            .foregroundStyle(V.textSecondary)
                            .padding(.horizontal, 11)
                            .padding(.vertical, 10)
                            .allowsHitTesting(false)
                    }
                }

            HStack(spacing: 10) {
                micButton
                speakToggle
                if voice.isSpeaking {
                    Button {
                        voice.stopSpeaking()
                    } label: {
                        Label("Stop speaking", systemImage: "speaker.slash.fill")
                            .font(.system(size: 12))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(V.textSecondary)
                }
                Spacer()
                if !draftTranscript.isEmpty {
                    Button("Clear") { draftTranscript = ""; typedCommand = "" }
                        .buttonStyle(.plain)
                        .font(.system(size: 12))
                        .foregroundStyle(V.textSecondary)
                }
                Button(isSending ? "Sending…" : "Send") { send() }
                    .buttonStyle(.borderedProminent)
                    .tint(V.accent)
                    .disabled(!canSend)
                    .keyboardShortcut(.return, modifiers: .command)
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 14)
    }

    private var recordingRow: some View {
        HStack(spacing: 8) {
            Circle().fill(V.danger).frame(width: 8, height: 8)
            Text("Listening — press Stop when you are done.")
                .font(.system(size: 12))
                .foregroundStyle(V.textSecondary)
            Spacer()
            Button("Stop") { voice.stopRecording() }
                .buttonStyle(.plain)
                .font(.system(size: 12))
                .foregroundStyle(V.danger)
        }
        .accessibilityLabel("Recording")
    }

    private var micButton: some View {
        Button {
            Task { await toggleRecording() }
        } label: {
            Label(voice.isRecording ? "Stop listening" : "Speak",
                  systemImage: voice.isRecording ? "mic.fill" : "mic")
                .font(.system(size: 12))
        }
        .buttonStyle(.plain)
        .foregroundStyle(voice.isRecording ? V.danger : V.textSecondary)
        .help(voice.isRecording ? "Stop listening" : "Start listening")
    }

    private var speakToggle: some View {
        Button {
            voice.speakReplies.toggle()
        } label: {
            Label(voice.speakReplies ? "Replies spoken" : "Replies silent",
                  systemImage: voice.speakReplies ? "speaker.wave.2.fill" : "speaker.slash")
                .font(.system(size: 12))
        }
        .buttonStyle(.plain)
        .foregroundStyle(voice.speakReplies ? V.accent : V.textSecondary)
        .help(voice.speakReplies
              ? "Vantage reads replies aloud. Press to make it silent."
              : "Vantage stays silent. Press to have replies read aloud.")
    }

    // MARK: - Actions

    private func toggleRecording() async {
        if voice.isRecording {
            voice.stopRecording()
            return
        }
        guard await voice.requestPermissions() else { return }
        draftTranscript = ""
        voice.startRecording()
    }

    private func send() {
        let text = composerText
        guard canSend, !text.isEmpty else { return }
        isSending = true
        // Cleared before awaiting so a second Return cannot resend the same
        // words. The rule lives in VoiceOperatorComposer so it is testable.
        let cleared = VoiceOperatorComposer.afterSend()
        draftTranscript = cleared.draftTranscript
        typedCommand = cleared.typedCommand
        Task {
            defer { isSending = false }
            if await chat.send(text) != nil {
                await speakLast()
            }
        }
    }

    /// Speak the last assistant message, if speech is on.
    ///
    /// `VoiceService.speak` already honours the preference, so this cannot make
    /// noise the person turned off. It is separate from `send` because the
    /// approval path produces a new message too.
    private func speakLast() async {
        guard let last = chat.conversation.messages.last,
              last.role == .assistant, !last.text.isEmpty else { return }
        voice.speak(last.text)
    }
}