import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Natural-language command interface using the backend's /command endpoint.
///
/// The screen is a transcript plus one composer. Everything it shows is a fact
/// the backend reported: an answer, a tool result, an action waiting for a
/// person, or a failure. The layout is deliberately calm — one accent bubble
/// for the person's own words, a bordered surface for an answer, an inset row
/// for a result, gold for an approval — so a reader can tell what happened
/// before reading a word.
struct CommandCenterView: View {

    /// Set by Vantage Anywhere when the palette handed a request to the full
    /// window. Taken once, then cleared, so navigating back does not resend it.
    @Binding var handoff: HandedOff?
    @State private var lastHandoff: UUID?

    @State private var inputText = ""
    @State private var errorMessage: String?

    init(handoff: Binding<HandedOff?> = .constant(nil)) {
        _handoff = handoff
    }

    /// The conversation, and the network round trip behind it.
    ///
    /// The service owns the session id, the transcript and the failure
    /// handling; this view owns the presentation. Splitting them is what lets
    /// the conversation survive a view rebuild, and what keeps the transport
    /// details out of the SwiftUI body.
    @StateObject private var chat = ConversationService()

    // Attachments. A file is chosen here, uploaded when the message it
    // belongs to is saved, and its result shown in the transcript.
    @State private var isAttaching = false
    @State private var isProcessingAttachment = false
    @State private var pendingAttachment: PendingAttachment?
    /// The conversation an attachment hangs off. An upload needs the id of a
    /// saved message, and a message does not exist until it is saved, so the
    /// conversation is created the first time one is needed and reused after.
    @State private var attachmentConversationId: String?

    // Approvals. A consequential action is held by the backend's gate until a
    // person answers, and that answer has to come from somewhere. Without
    // this the app could say "Requires confirmation" and then offer no way to
    // confirm -- the action could never complete from the desktop at all.
    @State private var pendingConfirmations: [APIModels.Confirmation] = []
    /// The last approvals refresh failed, so the cards below may be out of date.
    @State private var confirmationsUnread = false
    /// The confirmation whose decision is being sent. One at a time: the
    /// answer is a mutating call, and a second click must not send a second one.
    @State private var decidingConfirmationId: String?
    /// This app's session, so the backend can record who approved what.
    @State private var sessionId = "native"

    /// A send is in progress (attachment upload included), so a second gesture
    /// — Return and the send button both firing — cannot send twice.
    @State private var isSending = false

    /// The person is at the bottom of the transcript, so a new turn may follow
    /// itself down. Flipped by a sentinel row at the end of the transcript.
    @State private var isAtBottom = true
    /// The next turn was started by the person, so scrolling to it is what they
    /// asked for even if they had scrolled up to re-read something.
    @State private var followNextTurn = false

    @FocusState private var composerFocused: Bool

    @ObservedObject private var voice = VoiceService.shared

    /// The shared backend manager.
    ///
    /// Observed, not owned: a view-owned `@StateObject` releases the shared
    /// singleton when the view goes away, which would stop the health poll
    /// the whole app depends on. The manager already polls `/health`, and its
    /// report of `llm_enabled` / `ai_provider` is what tells this screen
    /// whether the AI path is answering.
    @ObservedObject private var backend = BackendManager.shared

    private let api = APIClient.shared

    private static let bottomAnchor = "command-center-bottom"
    private static let transcriptWidth: CGFloat = 760

    /// The transcript, as the conversation service holds it.
    private var messages: [ChatMessage] { chat.conversation.messages }

    /// What the header says about who is answering — read, never assumed.
    private var providerSubtitle: String {
        guard chat.capabilityKnown else {
            return "Checking which AI provider is answering…"
        }
        if chat.aiAvailable {
            return chat.providerName.isEmpty
                ? "Ask anything, or tell Vantage to do something."
                : "Conversational answers via \(chat.providerName)."
        }
        return "No AI provider is answering — tool actions still run."
    }

    private var providerPill: (kind: StatusKind, text: String) {
        guard chat.capabilityKnown else { return (.idle, "Checking provider…") }
        if chat.aiAvailable {
            return (.connected, chat.providerName.isEmpty ? "AI ready" : chat.providerName)
        }
        return (.setup, "Tool actions only")
    }

    private var trimmedInput: String {
        inputText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canSend: Bool {
        ( !trimmedInput.isEmpty || pendingAttachment != nil )
            && !chat.isThinking && !isSending && !isProcessingAttachment
    }

    var body: some View {
        VStack(spacing: 0) {
            header

            ScrollViewReader { proxy in
                GeometryReader { geo in
                    ScrollView {
                        if chat.conversation.isEmpty {
                            transcript
                                .frame(maxWidth: .infinity,
                                       minHeight: geo.size.height,
                                       alignment: .center)
                        } else {
                            transcript
                        }
                    }
                    .onChange(of: messages.count) { _ in
                        guard followNextTurn || isAtBottom else { return }
                        followNextTurn = false
                        scrollToBottom(proxy)
                    }
                    .onChange(of: chat.isThinking) { thinking in
                        guard thinking, isAtBottom else { return }
                        scrollToBottom(proxy)
                    }
                }
            }

            Rectangle()
                .fill(V.divider)
                .frame(height: 1)

            if voice.isSpeaking {
                speakingRow
            }

            composer
        }
        .background(V.background)
        .navigationTitle("Command Center")
        .onAppear {
            voice.onFinalTranscript = { text in
                inputText = text
                Task { await sendCommand() }
            }
            Task {
                // Read the provider's state when the screen appears, so the
                // notice is there before the first question rather than after
                // an answer that was not what it looked like.
                await chat.refreshCapability()
                await refreshConfirmations()
                // A request the palette handed over is answered here, once.
                await consumeHandoffIfNeeded()
            }
        }
        .onChange(of: handoff?.id) { _ in
            Task { await consumeHandoffIfNeeded() }
        }
        // The provider's state can change while this screen is open — a key
        // added in Settings, a backend restarted. The health poll the app
        // already runs is the natural trigger: re-read the capability when
        // the backend's state changes, and not on a timer of its own.
        .onChange(of: backend.health?.llm_enabled) { _ in
            Task { await chat.refreshCapability() }
        }
        .onChange(of: backend.health?.ai_provider) { _ in
            Task { await chat.refreshCapability() }
        }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(title: "Command Center", subtitle: providerSubtitle) {
            HStack(spacing: V.Space.s) {
                StatusPill(kind: providerPill.kind, text: providerPill.text)
                Button {
                    chat.startNewConversation()
                    attachmentConversationId = nil
                    pendingConfirmations = []
                    errorMessage = nil
                } label: {
                    Label("New conversation", systemImage: "square.and.pencil")
                }
                .buttonStyle(VSecondaryButtonStyle())
                .help("Start a new conversation")
                .disabled((messages.isEmpty && pendingConfirmations.isEmpty) || chat.isThinking)
            }
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.l)
        .padding(.bottom, V.Space.m)
    }

    // MARK: - Transcript

    /// The column: notices, live transcription, turns, approvals, progress.
    ///
    /// Extracted from `body` because Swift could not type-check the whole
    /// expression in reasonable time — it compiled inside the package but a
    /// standalone `swiftc` of this file failed with "unable to type-check this
    /// expression". Splitting it also makes each state visible on its own.
    @ViewBuilder
    private var transcript: some View {
        LazyVStack(alignment: .leading, spacing: V.Space.m) {
            if chat.conversation.isEmpty {
                EmptyConversationState(hasProvider: chat.aiAvailable,
                                       providerKnown: chat.capabilityKnown)
                    .padding(.vertical, V.Space.l)
            }

            // The AI path is off, or the provider is not answering. Said
            // plainly, before a question is answered, so the next answer is
            // not read as a model answer.
            if !chat.aiAvailable, !chat.aiUnavailableReason.isEmpty {
                Notice(
                    kind: .warning,
                    message: chat.aiUnavailableReason + "\n"
                        + "Answers will come from a Wikipedia lookup until this is sorted — "
                        + "it may return an article about a different subject than the one "
                        + "you asked about. Tool actions still run normally.",
                    actionTitle: "Check again",
                    action: { Task { await chat.refreshCapability() } }
                )
            }

            // A local failure that has no turn of its own (a refused file
            // picker, say). Failures that produced a turn are shown there —
            // drawing both put the same sentence on screen twice.
            if let error = errorMessage {
                Notice(kind: .error, message: error, actionTitle: "Dismiss") {
                    errorMessage = nil
                }
            }

            if let voiceError = voice.errorMessage {
                Notice(kind: .warning, message: voiceError, actionTitle: "Dismiss") {
                    voice.errorMessage = nil
                }
            }

            // A failed turn, with a way to try again. Retry re-sends the same
            // prompt (the service owns `lastPrompt`), so it is safe; it is
            // offered only while nothing is in flight. The failed turn stays
            // in the transcript — a retry does not hide that something failed.
            if chat.lastError != nil, !chat.lastPrompt.isEmpty, !chat.isThinking,
               !isSending {
                Notice(
                    kind: .error,
                    message: "The last message did not go through. Your question is still "
                        + "in the transcript, so you can read it back.",
                    actionTitle: "Retry",
                    action: { Task { await chat.retry() } }
                )
            }

            // Live transcription while the mic is open.
            if voice.isRecording && !voice.liveTranscript.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
                    Image(systemName: "mic.fill")
                        .font(V.Font.caption)
                        .foregroundStyle(V.accent)
                    Text("\u{201C}\(voice.liveTranscript)\u{201D}")
                        .font(V.Font.body)
                        .italic()
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, V.Space.m)
                .padding(.vertical, V.Space.s)
                .background(V.accentSoft,
                            in: RoundedRectangle(cornerRadius: V.Radius.control,
                                                 style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                        .strokeBorder(V.accent.opacity(0.22), lineWidth: 1)
                )
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Hearing: \(voice.liveTranscript)")
            }

            ForEach(messages) { msg in
                MessageBubble(message: msg)
                    .id(msg.id)
            }

            // Actions waiting for a person. The backend's gate holds a
            // consequential action until someone answers, and this is where
            // the answer comes from. The card is the one the Approvals screen
            // uses — one card, one place that knows how an approval is
            // presented, and one place that disables it while a decision is in
            // flight.
            if confirmationsUnread {
                Notice(
                    kind: .warning,
                    message: pendingConfirmations.isEmpty
                        ? "I could not check for actions waiting on you, so this is not an empty list."
                        : "I could not refresh this list. These approvals were waiting when I last looked.",
                    actionTitle: "Check",
                    action: { Task { await refreshConfirmations() } }
                )
            }
            ForEach(pendingConfirmations) { confirmation in
                ConfirmationCard(
                    confirmation: confirmation,
                    isDeciding: decidingConfirmationId == confirmation.id,
                    decisionsLocked: decidingConfirmationId != nil,
                    onApprove: { await resolve(confirmation, approve: true) },
                    onDeny: { await resolve(confirmation, approve: false) }
                )
                .id(confirmation.id)
            }

            if chat.isThinking {
                LoadingRow(text: "Vantage is working on it…")
                    .padding(.vertical, V.Space.xs)
                    .accessibilityLabel("Vantage is working on your request")
            }

            // The bottom sentinel. It is on screen only when the person is at
            // the bottom, so a reply never yanks them away from something they
            // scrolled up to read.
            Color.clear
                .frame(height: 1)
                .id(Self.bottomAnchor)
                .onAppear { isAtBottom = true }
                .onDisappear { isAtBottom = false }
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.vertical, V.Space.l)
        .frame(maxWidth: Self.transcriptWidth, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .center)
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        if V.motionAllowed {
            withAnimation(V.quick) {
                proxy.scrollTo(Self.bottomAnchor, anchor: .bottom)
            }
        } else {
            proxy.scrollTo(Self.bottomAnchor, anchor: .bottom)
        }
    }

    // MARK: - Composer

    /// "Speaking…" and a way to stop it. Without this, the voice control is
    /// only a preference: the person cannot tell whether a silent screen means
    /// the setting is off, the reply finished, or speech failed.
    private var speakingRow: some View {
        HStack(spacing: V.Space.s) {
            Image(systemName: "waveform")
                .font(V.Font.caption)
                .foregroundStyle(V.accent)
            Text("Speaking…")
                .font(V.Font.caption)
                .foregroundStyle(V.textSecondary)
            Button("Stop") { voice.stopSpeaking() }
                .buttonStyle(VQuietButtonStyle())
                .help("Stop the reply being spoken")
            Spacer()
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.xs)
        .frame(maxWidth: Self.transcriptWidth)
        .frame(maxWidth: .infinity, alignment: .center)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Vantage is speaking. Stop button available.")
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 0) {
            // The file waiting to go, and the progress of reading it.
            if let pending = pendingAttachment {
                HStack(spacing: V.Space.s) {
                    Image(systemName: "doc")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textSecondary)
                    Text(pending.name)
                        .font(V.Font.caption)
                        .foregroundStyle(V.text)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if isProcessingAttachment {
                        ProgressView().controlSize(.small)
                        Text("Reading…")
                            .font(V.Font.caption)
                            .foregroundStyle(V.textSecondary)
                    }
                    Spacer(minLength: V.Space.s)
                    Button {
                        pendingAttachment = nil
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(V.textTertiary)
                    }
                    .buttonStyle(.plain)
                    .help("Remove this file")
                    .accessibilityLabel("Remove attached file")
                }
                .padding(.horizontal, V.Space.m)
                .padding(.top, V.Space.s)
            }

            TextField("Ask anything, or tell Vantage to do something…",
                      text: $inputText, axis: .vertical)
                .textFieldStyle(.plain)
                .font(V.Font.message)
                .foregroundStyle(V.text)
                .lineLimit(1...6)
                .focused($composerFocused)
                .padding(.horizontal, V.Space.m)
                .padding(.top, V.Space.m)
                .padding(.bottom, V.Space.s)
                .onSubmit { submit() }

            Rectangle()
                .fill(V.divider)
                .frame(height: 1)

            HStack(spacing: V.Space.xs) {
                // Attach: choose a file. It is uploaded when the message it
                // belongs to is sent.
                IconAction(
                    symbol: "paperclip",
                    help: pendingAttachment == nil ? "Attach a file" : "Replace the attached file",
                    tint: pendingAttachment == nil ? V.textSecondary : V.accent
                ) {
                    isAttaching = true
                }
                .disabled(chat.isThinking)
                .fileImporter(
                    isPresented: $isAttaching,
                    allowedContentTypes: Self.allowedAttachmentTypes,
                    allowsMultipleSelection: false
                ) { result in
                    handlePickedFile(result)
                }

                // Mic toggle: record → transcribe → send.
                IconAction(
                    symbol: voice.isRecording ? "mic.fill" : "mic",
                    help: voice.isRecording ? "Stop listening" : "Speak",
                    tint: voice.isRecording ? V.danger : V.textSecondary,
                    isActive: voice.isRecording
                ) {
                    toggleVoice()
                }
                .disabled(chat.isThinking || isProcessingAttachment)

                // Spoken replies, on or off — the canonical preference lives
                // on VoiceService, so every surface agrees.
                InlineToggleChip(
                    title: "AI voice",
                    valueText: voice.speakReplies ? "On" : "Off",
                    isOn: $voice.speakReplies,
                    symbol: voice.speakReplies ? "speaker.wave.2.fill" : "speaker.slash"
                )
                .help(voice.speakReplies
                      ? "AI voice is on — click to mute replies"
                      : "AI voice is off — click to read replies aloud")

                Spacer(minLength: V.Space.s)

                sendButton
            }
            .padding(.horizontal, V.Space.s)
            .padding(.vertical, V.Space.xs + 2)
        }
        .background(
            RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                .fill(V.surface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                .strokeBorder(composerFocused ? V.accent.opacity(0.55) : V.border,
                              lineWidth: composerFocused ? 1.5 : 1)
        )
        .animation(V.motionAllowed ? V.quick : nil, value: composerFocused)
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.s)
        .padding(.bottom, V.Space.l)
        .frame(maxWidth: Self.transcriptWidth)
        .frame(maxWidth: .infinity, alignment: .center)
    }

    private var sendButton: some View {
        Button {
            submit()
        } label: {
            Group {
                if isSending || chat.isThinking {
                    ProgressView()
                        .controlSize(.small)
                        .tint(.white)
                } else {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(canSend ? .white : V.textTertiary)
                }
            }
            .frame(width: 28, height: 26)
            .background(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .fill(canSend ? V.accent
                                  : (chat.isThinking || isSending ? V.accent.opacity(0.45)
                                                                  : V.fill))
            )
            .contentShape(RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(!canSend)
        .keyboardShortcut(.return, modifiers: [])
        .help(chat.isThinking || isSending ? "Sending…" : "Send (Return)")
        .accessibilityLabel("Send message")
        .accessibilityValue(canSend ? "" : "Disabled")
    }

    private func submit() {
        guard canSend else { return }
        Task { await sendCommand() }
    }

    // MARK: - Attachments

    /// The kinds of file the backend's attachment routes accept.
    private static let allowedAttachmentTypes: [UTType] = {
        var types: [UTType] = [.png, .jpeg, .gif, .pdf, .plainText, .wav, .mp3]
        // WebP and MPEG audio have no constant UTType on every macOS
        // version, so they are named by their extension.
        for ext in ["webp", "mpga", "mpeg"] {
            if let type = UTType(filenameExtension: ext) { types.append(type) }
        }
        return types
    }()

    private func handlePickedFile(_ result: Result<[URL], Error>) {
        switch result {
        case .success(let urls):
            guard let url = urls.first else { return }
            // A picked file is a security-scoped resource; it has to be
            // claimed to be read, and the claim released afterwards.
            let scoped = url.startAccessingSecurityScopedResource()
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            pendingAttachment = PendingAttachment(
                url: url,
                name: url.lastPathComponent,
                mimeType: Self.mimeType(for: url)
            )
        case .failure(let error):
            errorMessage = error.localizedDescription
        }
    }

    /// The MIME type the backend's upload route accepts for this file.
    ///
    /// The backend refuses an unlisted type, so the type is derived from the
    /// extension rather than guessed by the system -- an unsupported file is
    /// reported here rather than after an upload that will be rejected.
    private static func mimeType(for url: URL) -> String {
        switch url.pathExtension.lowercased() {
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "gif": return "image/gif"
        case "webp": return "image/webp"
        case "pdf": return "application/pdf"
        case "txt": return "text/plain"
        case "wav": return "audio/wav"
        case "mp3": return "audio/mp3"
        case "mpeg", "mpga": return "audio/mpeg"
        default: return "application/octet-stream"
        }
    }

    private func toggleVoice() {
        if voice.isRecording {
            voice.stopRecording()
            return
        }
        Task {
            let allowed = await voice.requestPermissions()
            if allowed {
                voice.startRecording()
            }
        }
    }

    // MARK: - Sending


    /// Consume a request the palette handed over. Filling the field and sending
    /// it keeps this screen the single conversational surface — the palette does
    /// not answer questions itself.
    private func consumeHandoffIfNeeded() async {
        guard let pending = handoff, lastHandoff != pending.id else { return }
        lastHandoff = pending.id
        inputText = pending.text
        handoff = nil
        await sendCommand()
    }

    private func sendCommand() async {
        // Return can reach here twice (the field's submit and the send
        // button's shortcut). The first pass owns the send; the second must be
        // a no-op rather than a duplicate turn — and an attachment-only send
        // has no cleared text to make that happen on its own.
        guard !isSending, !chat.isThinking else { return }
        let text = trimmedInput
        let attachment = pendingAttachment
        // A message with only a file is still a message: the person attached
        // something to be read.
        guard !text.isEmpty || attachment != nil else { return }

        isSending = true
        defer { isSending = false }
        inputText = ""
        errorMessage = nil

        // The attachment goes first, and it needs a message to belong to.
        // The conversation is created the first time one is needed and
        // reused after, so a second attachment does not start a second
        // conversation.
        if let attachment {
            await processAttachment(attachment, messageText: text)
        }
        pendingAttachment = nil

        // The turn itself goes through the conversation service, which owns
        // the session id. That is what makes a follow-up question resolve
        // against what was just said.
        guard !text.isEmpty else { return }
        followNextTurn = true
        if let answer = await chat.send(text) {
            // Speak the reply in the signed app (AVSpeechSynthesizer), so the
            // response is audible without any backend TTS dependency.
            //
            // This is the ONLY speech call for a /command turn. The other
            // `voice.speak` in this view (in `resolve`, below) speaks the
            // backend's reply to an *approval* — a different response from a
            // different endpoint that is not added by `chat.send`, so no single
            // assistant message is ever spoken twice.
            if answer.role == .assistant, !answer.text.isEmpty {
                voice.speak(answer.text)
            }
        }
    }

    /// Upload the chosen file, have the backend read it, and show what it
    /// found.
    ///
    /// The order is the contract the dashboard uses too: a conversation, a
    /// saved message, the upload against that message, the processing, and
    /// then the result. A failure is reported as a failure -- the person is
    /// told the file could not be read rather than shown a blank card.
    private func processAttachment(_ pending: PendingAttachment, messageText: String) async {
        isProcessingAttachment = true
        defer { isProcessingAttachment = false }

        do {
            // Reuse the conversation when there is one, so a second
            // attachment joins the same thread rather than starting another.
            let chatId: String
            if let existing = attachmentConversationId {
                chatId = existing
            } else {
                let chat: APIModels.ChatCreated = try await api.post(
                    "/ai/chats", body: ["title": "Command Center"]
                )
                chatId = chat.id
                attachmentConversationId = chatId
            }

            let message: APIModels.MessageSaved = try await api.post(
                "/ai/chats/\(chatId)/messages",
                body: ["role": "user", "content": messageText.isEmpty ? "(attachment)" : messageText]
            )

            let upload: APIModels.AttachmentUpload = try await api.upload(
                "/ai/attachments/upload",
                fileURL: pending.url,
                fileName: pending.name,
                mimeType: pending.mimeType,
                fields: ["message_id": message.id]
            )
            guard let attachmentId = upload.attachment_id else {
                throw APIError.transport("The upload did not name the attachment.")
            }

            let processed: APIModels.AttachmentProcessing = try await api.post(
                "/ai/process-attachments", body: ["attachment_ids": [attachmentId]]
            )
            guard let result = processed.results?.first(where: { $0.attachment_id == attachmentId })
                    ?? processed.results?.first else {
                throw APIError.transport("The backend did not report a result for the file.")
            }

            let summary = APIModels.AttachmentResult(
                status: result.status,
                type: result.type,
                summary: result.summary,
                text: result.text,
                text_note: result.text_note,
                text_lines: result.text_lines,
                reason: result.reason,
                error: result.error
            )
            chat.appendResult(
                summary.displayText,
                isError: result.status == "error"
            )
        } catch {
            // The failure is a turn of its own. Setting `errorMessage` too
            // would draw the same sentence a second time as a banner.
            chat.appendResult(
                "Could not read \(pending.name): \(error.localizedDescription)",
                isError: true
            )
        }
    }

    // MARK: - Approvals

    /// Load the actions waiting for a person.
    ///
    /// A failure to load them is not shown as a big error: the list is a
    /// convenience, and the gate is still holding the action whether or not
    /// this app can show it. A message is queued by the response itself, so
    /// the approval the person just asked for still appears.
    ///
    /// But a failed read must not *empty* the list. Clearing it removed an
    /// approval the backend was still holding from the only place it could be
    /// answered, so the action stayed blocked with nothing on screen to say
    /// why. The last known list is kept and its staleness is flagged instead.
    private func refreshConfirmations() async {
        do {
            pendingConfirmations = try await api.pendingConfirmations()
            confirmationsUnread = false
        } catch {
            // Not fatal: the action is still held by the backend. Say the list
            // could not be refreshed rather than implying nothing is waiting.
            confirmationsUnread = true
        }
    }

    /// Approve or deny a pending action, and report what happened.
    ///
    /// One decision at a time: the card disables both buttons while
    /// `decidingConfirmationId` is set, so a double-click cannot send two
    /// answers for one action.
    private func resolve(_ confirmation: APIModels.Confirmation, approve: Bool) async {
        guard decidingConfirmationId == nil else { return }
        decidingConfirmationId = confirmation.id
        defer { decidingConfirmationId = nil }

        do {
            let response = try await api.resolveConfirmation(
                confirmation.confirmation_id, approve: approve, sessionId: sessionId)
            let body = response.reply?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let text = body.isEmpty ? (approve ? "Approved." : "Denied.") : body
            chat.appendResult(
                text,
                isError: !approve,
                meta: ChatMessage.MessageMeta(
                    model: response.model_used ?? "",
                    durationMS: response.duration_ms,
                    blocked: response.decision == "BLOCK",
                    sessionID: response.session_id ?? ""
                )
            )
            if approve, let reply = response.reply, !reply.isEmpty {
                voice.speak(reply)
            }
        } catch {
            // Appended as a turn; no second banner with the same sentence.
            chat.appendResult(
                "Failed: \(error.localizedDescription)",
                isError: true
            )
        }
        await refreshConfirmations()
    }
}

// MARK: - Message model

/// A file the user has chosen but not yet sent.
///
/// It carries the URL, the name to show, and the MIME type the backend's
/// upload route accepts. The type is decided when the file is chosen, so an
/// unsupported file is refused here rather than after an upload that will be
/// rejected.
struct PendingAttachment {
    let url: URL
    let name: String
    let mimeType: String
}

// MARK: - Transcript rows

/// One turn in the transcript, drawn by role.
///
/// The five shapes are deliberately distinct:
/// * **user** — right-aligned in an accent-tinted bubble;
/// * **assistant** — a bordered surface with the facts of the turn and a Copy
///   action;
/// * **tool** — a compact inset row with a monospaced label, because a result
///   of something Vantage *did* is not the same as an answer;
/// * **error** — a danger-toned row;
/// * an assistant turn with no visible text and no meta draws nothing.
struct MessageBubble: View {
    let message: ChatMessage

    var body: some View {
        switch message.role {
        case .user:
            userRow
        case .assistant:
            if message.text.isEmpty && message.meta.isEmpty {
                EmptyView()
            } else {
                assistantRow
            }
        case .tool:
            if message.text.isEmpty {
                EmptyView()
            } else {
                toolRow
            }
        case .error:
            errorRow
        }
    }

    private var userRow: some View {
        HStack(alignment: .top, spacing: 0) {
            Spacer(minLength: 64)
            Text(message.text)
                .font(V.Font.message)
                .foregroundStyle(V.text)
                .textSelection(.enabled)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, V.Space.m)
                .padding(.vertical, V.Space.s + 1)
                .background(
                    RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                        .fill(V.accentSoft)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                        .strokeBorder(V.accent.opacity(0.22), lineWidth: 1)
                )
                .frame(maxWidth: 540, alignment: .trailing)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("You said: \(message.text)")
    }

    private var assistantRow: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text(message.text.isEmpty
                 ? "Vantage answered with no text."
                 : message.text)
                .font(V.Font.message)
                .foregroundStyle(V.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)

            HStack(spacing: V.Space.s) {
                if !message.meta.isEmpty {
                    MessageCaption(meta: message.meta)
                }
                Spacer(minLength: V.Space.s)
                CopyMessageButton(text: message.text)
            }
        }
        .padding(.horizontal, V.Space.m + 2)
        .padding(.vertical, V.Space.m)
        .background(
            RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                .fill(V.surface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                .strokeBorder(V.border, lineWidth: 1)
        )
        .frame(maxWidth: 620, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Vantage said: \(message.text)")
    }

    private var toolRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Text("RESULT")
                .font(V.Font.mono.weight(.semibold))
                .tracking(0.6)
                .foregroundStyle(V.textTertiary)
                .frame(width: 52, alignment: .leading)
            Text(message.text)
                .font(V.Font.body)
                .foregroundStyle(V.textSecondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, V.Space.m)
        .padding(.vertical, V.Space.s)
        .background(
            RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                .fill(V.surfaceAlt)
        )
        .overlay(
            RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                .strokeBorder(V.border, lineWidth: 1)
        )
        .frame(maxWidth: 620, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Result: \(message.text)")
    }

    private var errorRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Image(systemName: "exclamationmark.octagon.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(V.danger)
            Text(message.text.isEmpty
                 ? "That turn failed before it produced an answer."
                 : message.text)
                .font(V.Font.body)
                .foregroundStyle(V.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, V.Space.m)
        .padding(.vertical, V.Space.s + 2)
        .background(
            RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                .fill(V.dangerSoft)
        )
        .overlay(
            RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                .strokeBorder(V.danger.opacity(0.25), lineWidth: 1)
        )
        .frame(maxWidth: 620, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Error: \(message.text)")
    }
}

/// Copy an answer to the pasteboard, and say so for a moment.
struct CopyMessageButton: View {
    let text: String

    @State private var copied = false

    var body: some View {
        Button {
            let board = NSPasteboard.general
            board.clearContents()
            board.setString(text, forType: .string)
            copied = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) {
                copied = false
            }
        } label: {
            HStack(spacing: V.Space.xs) {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 10, weight: .semibold))
                Text(copied ? "Copied" : "Copy")
                    .font(V.Font.caption)
            }
        }
        .buttonStyle(VQuietButtonStyle(tint: copied ? V.accent : V.textSecondary))
        .help("Copy this answer")
        .accessibilityLabel(copied ? "Copied" : "Copy answer")
        .accessibilityValue(copied ? "Copied to the clipboard" : "")
    }
}

/// The small line under an answer: who answered, how long it took, and what
/// is still waiting.
///
/// Each fact is only shown when the server actually sent it, so an empty
/// caption is a real absence rather than a rendering accident.
struct MessageCaption: View {
    let meta: ChatMessage.MessageMeta

    var body: some View {
        HStack(spacing: V.Space.xs + 2) {
            if meta.awaitingApproval {
                Label("Waiting for your approval", systemImage: "exclamationmark.shield.fill")
                    .foregroundStyle(V.warning)
            }
            if meta.blocked {
                Label("Blocked", systemImage: "hand.raised.fill")
                    .foregroundStyle(V.danger)
            }
            if !meta.model.isEmpty {
                // The server's own label, which names the provider and model
                // ("9router:ag/gemini-2.5-flash").
                //
                // Anything else came from the rule router, and the attribution
                // has to match the turn. A tool action ("Check my battery" ->
                // "Your laptop battery is at 100%") was captioned "Vantage (no
                // AI provider)", which claimed a capability gap on a turn that
                // did exactly what was asked; a declined question was captioned
                // the same, which hid the reason the server actually gave.
                // Neither is shown on a held action either -- the approval is
                // the point of that row, and the label read as though the
                // approval were degraded.
                if meta.isModelAnswer {
                    Text(meta.model)
                } else if !meta.awaitingApproval && !meta.blocked {
                    Text("Vantage")
                }
            }
            if let ms = meta.durationMS {
                Text("\(ms) ms")
            }
        }
        .font(V.Font.caption)
        .foregroundStyle(V.textTertiary)
        .lineLimit(1)
        .truncationMode(.middle)
    }
}

/// What the transcript shows before the first message.
///
/// An empty transcript with no explanation reads as a broken screen. This
/// says what Vantage can do, so the first thing a person sees is an
/// invitation rather than a void.
struct EmptyConversationState: View {
    /// False when the backend has already said there is no provider to answer
    /// with. The card used to say "using your configured AI provider"
    /// unconditionally, directly above a notice saying none was configured --
    /// the screen contradicting itself in one viewport.
    var hasProvider: Bool = true
    /// False when the capability check has never answered, so `hasProvider`
    /// is the optimistic default rather than something read.
    var providerKnown: Bool = true

    private var mayChat: Bool { hasProvider && providerKnown }

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            HStack(spacing: V.Space.s) {
                Image(systemName: "sparkles")
                    .font(.system(size: 15, weight: .light))
                    .foregroundStyle(V.gold)
                Text("Ask Vantage anything")
                    .font(V.Font.cardTitle)
                    .foregroundStyle(V.text)
            }

            Text(providerKnown
                 ? (hasProvider
                    ? "Vantage answers general questions using your configured AI provider, "
                      + "and runs tools for you when you ask it to do something."
                    : "Vantage can run tools for you when you ask it to do something. "
                      + "Conversational answers need an AI provider — see the notice below.")
                 : "Vantage runs tools for you when you ask it to do something. I have not "
                   + "been able to check which AI provider, if any, is answering.")
                .font(V.Font.body)
                .foregroundStyle(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            VStack(alignment: .leading, spacing: V.Space.s) {
                // `mayChat` is the safe side of an unread provider: offer only
                // what works either way. Promising a conversational example
                // when nothing could be read would put the card back in the
                // business of asserting what it has not checked.
                Example(label: "Ask", text: mayChat
                        ? "Explain how solar panels generate electricity."
                        : "What is on my calendar today?")
                // With no provider the follow-up and the lookup examples are
                // exactly what cannot work: a follow-up needs a model to reason
                // over the thread, and this screen's whole point with no
                // provider is that tool actions still run.
                Example(label: "Follow up", text: mayChat
                        ? "Explain the second part in simpler words."
                        : "Check my battery.")
                Example(label: "Act", text: mayChat
                        ? "Check my battery."
                        : "Search my downloads for invoices.")
            }
            .padding(.top, V.Space.xxs)
        }
        // The card fills the column: it shares an edge with the composer and
        // with the first message that replaces it, and it cannot be clipped
        // when the window narrows.
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, V.Space.l)
        .padding(.vertical, V.Space.l)
        .background(
            RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                .fill(V.surfaceAlt)
        )
        .overlay(
            RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                .strokeBorder(V.border, lineWidth: 1)
        )
    }

    /// One example, so the three shapes of a conversation are legible.
    private struct Example: View {
        let label: String
        let text: String

        var body: some View {
            HStack(alignment: .firstTextBaseline, spacing: V.Space.m) {
                Text(label.uppercased())
                    .font(V.Font.caption.weight(.semibold))
                    .tracking(0.4)
                    .foregroundStyle(V.textTertiary)
                    .lineLimit(1)
                    .fixedSize()
                    .frame(width: 76, alignment: .leading)
                Text(text)
                    .font(V.Font.body)
                    .foregroundStyle(V.text)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}