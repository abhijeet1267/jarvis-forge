import SwiftUI
import AppKit
import UniformTypeIdentifiers

/// Documents — read, understand and act on files you explicitly choose.
///
/// The screen's honesty rules, each visible in the layout:
/// * the file is named and its *identity* is shown before anything is sent;
/// * "Overview" is marked **Local**, and every AI action says which provider will
///   receive the text — before it is pressed, not in a footnote afterwards;
/// * an extracted action list is a **draft** with editable titles and dates, and
///   nothing enters the planner until the person presses Add;
/// * a failure is a failure — an unreadable PDF says so rather than showing an
///   empty summary.
struct DocumentsView: View {

    @ObservedObject private var runtime = DocumentRuntime.shared
    @ObservedObject private var backend = BackendManager.shared
    @ObservedObject private var control = MacControlCoordinator.shared

    @State private var isPicking = false
    @State private var isPickingSecond = false
    @State private var selected: UUID?
    @State private var question = ""
    @State private var working: DocumentRequest?
    @State private var pickError: String?
    @State private var status: Status = .idle
    @State private var savingResult: DocumentResult?
    @State private var addingProposals = false

    private let service = DocumentService()

    private enum Status: Equatable {
        case idle
        case done(String)
        case problem(String)
    }

    private var availability: DocumentService.ProviderAvailability {
        service.providerAvailability(from: backend.health)
    }

    private var session: DocumentSession? {
        if let selected, let found = runtime.session(selected) { return found }
        return runtime.primary
    }

    private var comparison: DocumentSession? {
        guard runtime.sessions.count > 1 else { return nil }
        return runtime.sessions.dropFirst().first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: V.Space.l) {
                    if let pickError {
                        Notice(kind: .error, message: pickError, actionTitle: "Dismiss") {
                            self.pickError = nil
                        }
                    }
                    statusLine
                    if runtime.sessions.isEmpty {
                        EmptyState(symbol: "doc.text.magnifyingglass",
                                   title: "Choose a document",
                                   message: "Vantage reads the file you select and nothing else. "
                                     + "PDF, plain text, Markdown, rich text, HTML, CSV and "
                                     + "source files are read on this Mac; only a summary or a "
                                     + "question sends the text to your AI provider, and the "
                                     + "button says so first.",
                                   actionTitle: "Choose a file…") { isPicking = true }
                    } else {
                        sourceCards
                        if let session { actions(session) }
                        if let session, !session.results.isEmpty { results(session) }
                        proposalsSection
                    }
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.bottom, V.Space.section)
                .frame(maxWidth: 820, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .background(V.background)
        .task { consumePrefill() }
        .fileImporter(isPresented: $isPicking, allowedContentTypes: allowedTypes,
                      allowsMultipleSelection: false) { result in
            handlePick(result, comparison: false)
        }
        .fileImporter(isPresented: $isPickingSecond, allowedContentTypes: allowedTypes,
                      allowsMultipleSelection: false) { result in
            handlePick(result, comparison: true)
        }
        .fileExporter(isPresented: Binding(get: { savingResult != nil },
                                           set: { if !$0 { savingResult = nil } }),
                      document: savingDocument, contentType: .plainText,
                      defaultFilename: savingResult.map(exportName)) { result in
            switch result {
            case .success(let url):
                status = .done("Saved to \(url.lastPathComponent).")
            case .failure(let error):
                status = .problem("Could not save: \(error.localizedDescription)")
            }
            savingResult = nil
        }
    }

    /// The formats the picker offers. Derived from `DocumentKind` so a kind the
    /// extractor cannot read can never appear here.
    private var allowedTypes: [UTType] {
        DocumentKind.allExtensions.compactMap { UTType(filenameExtension: $0) }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(title: "Documents",
                   subtitle: "Read and understand a file you choose.") {
            HStack(spacing: V.Space.s) {
                StatusPill(kind: availability.available ? .connected : .setup,
                           text: availability.available
                             ? "AI: \(availability.name)"
                             : "Local reading only")
                Button("Choose file…") { isPicking = true }
                    .buttonStyle(VSecondaryButtonStyle())
                if !runtime.sessions.isEmpty {
                    Button("Close all") {
                        runtime.clear()
                        selected = nil
                        status = .idle
                    }
                    .buttonStyle(VSecondaryButtonStyle())
                    .help("Remove the open documents from this screen. The files are not changed.")
                }
            }
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.section)
        .padding(.bottom, V.Space.l)
        .frame(maxWidth: 820)
        .frame(maxWidth: .infinity, alignment: .center)
    }

    @ViewBuilder
    private var statusLine: some View {
        switch status {
        case .idle: EmptyView()
        case .done(let message):
            Label(message, systemImage: "checkmark.circle.fill")
                .font(V.Font.caption).foregroundStyle(V.accent)
        case .problem(let message):
            Notice(kind: .error, message: message, actionTitle: "Dismiss") { status = .idle }
        }
    }

    // MARK: - Sources

    private var sourceCards: some View {
        HStack(alignment: .top, spacing: V.Space.s) {
            ForEach(runtime.sessions) { open in
                sourceCard(open, isSelected: open.id == (session?.id))
                    .onTapGesture { selected = open.id }
            }
            if runtime.sessions.count == 1 {
                Button {
                    isPickingSecond = true
                } label: {
                    VStack(spacing: V.Space.xs) {
                        Image(systemName: "plus").font(.system(size: 14, weight: .medium))
                        Text("Add to compare").font(V.Font.caption)
                    }
                    .frame(maxWidth: .infinity, minHeight: 78)
                    .foregroundStyle(V.textSecondary)
                    .background(V.surfaceAlt, in: RoundedRectangle(cornerRadius: V.Radius.card))
                    .overlay(RoundedRectangle(cornerRadius: V.Radius.card)
                        .strokeBorder(V.border, style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
                }
                .buttonStyle(.plain)
                .help("Choose a second document to compare with the first")
            }
        }
    }

    private func sourceCard(_ open: DocumentSession, isSelected: Bool) -> some View {
        Card(tone: isSelected ? .accent : .plain) {
            VStack(alignment: .leading, spacing: V.Space.xs) {
                HStack(spacing: V.Space.xs) {
                    Image(systemName: "doc.text")
                        .font(.system(size: 11))
                        .foregroundStyle(isSelected ? V.accent : V.textSecondary)
                    Text(open.sourceName)
                        .font(V.Font.label)
                        .foregroundStyle(V.text)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 0)
                    Button {
                        runtime.remove(open.id)
                        if selected == open.id { selected = runtime.primary?.id }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 10))
                            .foregroundStyle(V.textTertiary)
                    }
                    .buttonStyle(.plain)
                    .help("Close this document (the file is not changed)")
                }
                Text(open.document.source.kind.displayName
                     + " · " + byteLabel(open.document.source.byteSize))
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                Text(open.document.method.description)
                    .font(V.Font.caption)
                    .foregroundStyle(open.document.method.isReliable ? V.textTertiary : V.warning)
                    .fixedSize(horizontal: false, vertical: true)
                if open.document.truncated {
                    Label("Longer than the read limit — only the first part is used for AI.",
                          systemImage: "scissors")
                        .font(V.Font.caption).foregroundStyle(V.warning)
                }
                HStack(spacing: V.Space.xs) {
                    Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([open.document.source.url]) }
                        .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                        .help("Show this file in Finder")
                    Button("Open") { NSWorkspace.shared.open(open.document.source.url) }
                        .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                        .help("Open this file in its own app")
                }
            }
        }
    }

    // MARK: - Actions

    private func actions(_ session: DocumentSession) -> some View {
        SectionBlock(title: "What would you like to do?") {
            VStack(alignment: .leading, spacing: V.Space.m) {
                if !availability.available, !availability.reason.isEmpty {
                    Notice(kind: .warning, message: availability.reason)
                }

                Card {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        HStack(spacing: V.Space.s) {
                            actionButton(.overview, session: session, local: true)
                            Text("Read on this Mac. Nothing is sent anywhere.")
                                .font(V.Font.caption).foregroundStyle(V.textTertiary)
                        }
                        Divider().overlay(V.divider)
                        // AI actions, each labelled with where the text goes.
                        HStack(spacing: V.Space.s) {
                            actionButton(.summarize, session: session)
                            actionButton(.extractKeyPoints, session: session)
                            actionButton(.simplify, session: session)
                            actionButton(.actionItems, session: session)
                        }
                        Text("These send \(session.sourceName)'s text (up to "
                             + "\(formatNumber(24_000)) characters) to "
                             + (availability.name.isEmpty ? "your configured AI provider"
                                                          : availability.name)
                             + ". Vantage does not upload anything on its own.")
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            .fixedSize(horizontal: false, vertical: true)
                        if comparison != nil {
                            HStack(spacing: V.Space.s) {
                                Button {
                                    run(.compare, session: session)
                                } label: {
                                    Label("Compare the two documents", systemImage: "rectangle.split.2x1")
                                }
                                .buttonStyle(VSecondaryButtonStyle())
                                .disabled(working != nil || !availability.available)
                                if working == .compare { ProgressView().controlSize(.small) }
                            }
                        }
                    }
                }

                Card {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        HStack(spacing: V.Space.s) {
                            TextField("Ask something about this document…", text: $question)
                                .textFieldStyle(.plain)
                                .font(V.Font.body)
                                .padding(.horizontal, V.Space.m)
                                .padding(.vertical, 7)
                                .background(V.surfaceAlt,
                                            in: RoundedRectangle(cornerRadius: V.Radius.control))
                                .onSubmit { ask(session) }
                            Button("Ask") { ask(session) }
                                .buttonStyle(VPrimaryButtonStyle())
                                .disabled(question.trimmingCharacters(in: .whitespaces).isEmpty
                                          || working != nil || !availability.available)
                        }
                        Text(availability.available
                             ? "The question and the document's text go to \(availability.name)."
                             : "Asking needs an AI provider. Local reading still works.")
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    }
                }
            }
        }
    }

    private func actionButton(_ request: DocumentRequest, session: DocumentSession,
                              local: Bool = false) -> some View {
        Button {
            run(request, session: session)
        } label: {
            HStack(spacing: V.Space.xs) {
                Image(systemName: request.symbol).font(.system(size: 10))
                Text(request.title)
                if local {
                    Text("Local")
                        .font(.system(size: 9, weight: .semibold))
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(V.accentSoft, in: Capsule())
                        .foregroundStyle(V.accent)
                }
            }
        }
        .buttonStyle(VSecondaryButtonStyle())
        .disabled(working != nil || (!local && !availability.available))
        .help(local ? "Read locally — no network request"
                    : "Sends this document's text to \(availability.name)")
    }

    // MARK: - Results

    private func results(_ session: DocumentSession) -> some View {
        SectionBlock(title: "Results") {
            VStack(spacing: V.Space.s) {
                ForEach(session.results.reversed()) { result in
                    Card(tone: result.isError ? .danger : .plain) {
                        VStack(alignment: .leading, spacing: V.Space.s) {
                            HStack(spacing: V.Space.s) {
                                Image(systemName: result.request.symbol)
                                    .font(.system(size: 11)).foregroundStyle(V.textSecondary)
                                Text(result.request.title)
                                    .font(V.Font.label).foregroundStyle(V.text)
                                if case .question = result.request {
                                    Text("“\(result.sourceName)”")
                                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                                }
                                Spacer(minLength: V.Space.s)
                                Text(result.isLocal ? "Local"
                                     : result.answeredBy)
                                    .font(V.Font.caption)
                                    .foregroundStyle(result.isLocal ? V.accent : V.textTertiary)
                                if result.durationMS > 0 {
                                    Text("\(result.durationMS) ms")
                                        .font(V.Font.mono).foregroundStyle(V.textTertiary)
                                }
                            }
                            Text(result.text)
                                .font(V.Font.body)
                                .foregroundStyle(V.text)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                            HStack(spacing: V.Space.s) {
                                if !result.extractionNote.isEmpty {
                                    Text(result.extractionNote)
                                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                                }
                                Spacer(minLength: V.Space.s)
                                CopyMessageButton(text: result.text)
                                if !result.isError {
                                    Button {
                                        savingResult = result
                                    } label: {
                                        Label("Save…", systemImage: "square.and.arrow.down")
                                    }
                                    .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                                    .help("Save this result to a file you choose")
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - Action items

    @ViewBuilder
    private var proposalsSection: some View {
        if let session, !session.proposals.isEmpty || session.proposalProblem != nil {
            SectionBlock(title: "Proposed tasks — review before adding") {
                Card(tone: .gold) {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        if let problem = session.proposalProblem {
                            Text(problem)
                                .font(V.Font.body).foregroundStyle(V.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        ForEach(proposalBinding(session)) { $proposal in
                            VStack(alignment: .leading, spacing: V.Space.xs) {
                                HStack(spacing: V.Space.s) {
                                    Toggle("", isOn: $proposal.isApproved)
                                        .toggleStyle(.checkbox)
                                        .labelsHidden()
                                    TextField("Task title", text: $proposal.title)
                                        .textFieldStyle(.plain)
                                        .font(V.Font.body)
                                    Picker("", selection: $proposal.dueConfidence) {
                                        Text("No date").tag(ProposedAction.DueConfidence.none)
                                        Text("Date stated").tag(ProposedAction.DueConfidence.stated)
                                        Text("Inferred").tag(ProposedAction.DueConfidence.inferred)
                                    }
                                    .labelsHidden().frame(width: 120)
                                    if proposal.hasDate, let due = proposal.dueDate {
                                        Text(due.formatted(date: .abbreviated, time: .omitted))
                                            .font(V.Font.caption).foregroundStyle(V.textSecondary)
                                    }
                                }
                                if !proposal.owner.isEmpty {
                                    Text("Owner: \(proposal.owner)")
                                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                                        .padding(.leading, 26)
                                }
                                if proposal.dueConfidence == .inferred {
                                    Label(proposal.dueConfidence.label, systemImage: "questionmark.circle")
                                        .font(V.Font.caption).foregroundStyle(V.warning)
                                        .padding(.leading, 26)
                                }
                            }
                            .padding(.vertical, 2)
                        }
                        // A document's wording is prose, not a Vantage action, so it
                        // is never turned into executable steps on its own. The
                        // flow handoff is offered only when the action *is* one
                        // Vantage can perform (it names an app it can open), and
                        // even then it opens an unsaved draft.
                        if let automatable = automatableFlow(for: session) {
                            HStack(spacing: V.Space.s) {
                                Label("“\(automatable.name)” can be a flow — this action names an "
                                      + "app Vantage can open.", systemImage: "square.stack.3d.up")
                                    .font(V.Font.caption).foregroundStyle(V.textSecondary)
                                Spacer()
                                Button("Draft as flow…") {
                                    AppNavigation.shared.go(.flows, flowDraft: automatable)
                                }
                                .buttonStyle(VSecondaryButtonStyle())
                            }
                        }
                        HStack {
                            Text("\(session.proposals.filter(\.isApproved).count) selected. "
                                 + "These go into your planner — nothing is created until you add them.")
                                .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            Spacer()
                            if addingProposals { ProgressView().controlSize(.small) }
                            Button("Add selected to planner") {
                                addApproved(session)
                            }
                            .buttonStyle(VPrimaryButtonStyle())
                            .disabled(addingProposals
                                      || session.proposals.allSatisfy { !$0.isApproved })
                        }
                    }
                }
            }
        }
    }

    /// A flow draft from an approved action item, when the item really is an
    /// action Vantage can perform.
    ///
    /// The authoring path is the same one typed sentences use, and an item that
    /// does not map to a real action returns nil — which is the common case for
    /// document wording ("send the deck to Priya" is not a Mac action). Nothing
    /// here saves or runs anything.
    private func automatableFlow(for session: DocumentSession) -> Flow? {
        let approved = session.proposals.filter(\.isApproved)
        guard !approved.isEmpty else { return nil }
        let authoring = FlowAuthoring(
            applications: MacControlCoordinator.shared.installedApplications)
        let steps = approved.compactMap { proposal -> [FlowStep]? in
            guard case .draft(let flow) = authoring.interpret(
                "create a flow that \(proposal.title)", store: .shared) else { return nil }
            return flow.steps.isEmpty ? nil : flow.steps
        }.flatMap { $0 }
        guard !steps.isEmpty else { return nil }
        let base = (session.sourceName as NSString).deletingPathExtension
        return Flow(name: "\(base) actions", description:
                        "Drafted from \(session.sourceName). Review before saving.",
                    steps: steps,
                    failurePolicy: PreferenceRuntime.shared.store.defaultFlowPolicy() ?? .stop)
    }

    /// A binding into the runtime's session, so the editor's text fields write
    /// back to the stored proposals rather than a copy.
    private func proposalBinding(_ session: DocumentSession) -> Binding<[ProposedAction]> {
        Binding(
            get: { runtime.session(session.id)?.proposals ?? [] },
            set: { newValue in
                guard var open = runtime.session(session.id) else { return }
                open.proposals = newValue
                runtime.update(open)
            })
    }

    // MARK: - Pick / run

    private func handlePick(_ result: Result<[URL], Error>, comparison: Bool) {
        switch result {
        case .success(let urls):
            guard let url = urls.first else { return }
            do {
                let source = try DocumentExtractor.describe(url)
                let extracted = try DocumentExtractor.extract(source)
                if comparison {
                    runtime.addComparison(extracted)
                } else {
                    runtime.setPrimary(extracted)
                    selected = runtime.primary?.id
                }
                pickError = nil
                status = .idle
            } catch let error as DocumentError {
                pickError = error.errorDescription
            } catch {
                pickError = "Could not read \(url.lastPathComponent): \(error.localizedDescription)"
            }
        case .failure(let error):
            pickError = error.localizedDescription
        }
    }

    private func run(_ request: DocumentRequest, session: DocumentSession) {
        guard working == nil else { return }
        working = request
        Task {
            defer { working = nil }
            let result = await service.run(request, on: session.document,
                                           second: comparison?.document)
            if var open = runtime.session(session.id) {
                open.results.append(result)
                runtime.update(open)
            }
            if request == .actionItems, !result.isError {
                let parsed = ActionItemParser().parse(result.text, source: session.document.source)
                if var open = runtime.session(session.id) {
                    open.proposals = parsed.actions.map {
                        var action = $0
                        action.isApproved = true
                        return action
                    }
                    open.proposalProblem = parsed.problem
                    runtime.update(open)
                }
            }
        }
    }

    private func ask(_ session: DocumentSession) {
        let text = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        question = ""
        run(.question(text), session: session)
    }

    private func addApproved(_ session: DocumentSession) {
        let approved = session.proposals.filter(\.isApproved)
        guard !approved.isEmpty else { return }
        addingProposals = true
        Task {
            var created = 0
            var failures: [String] = []
            for proposal in approved {
                do {
                    _ = try await APIClient.shared.createPlannerTask(
                        title: proposal.title, priority: "normal",
                        detail: proposal.detail + (proposal.owner.isEmpty
                                                   ? "" : "\nOwner: \(proposal.owner)"),
                        dueAt: proposal.dueConfidence == .stated ? proposal.dueDate : nil)
                    created += 1
                } catch {
                    failures.append("“\(proposal.title)”: \(error.localizedDescription)")
                }
            }
            addingProposals = false
            if var open = runtime.session(session.id) {
                open.proposals.removeAll { $0.isApproved }
                runtime.update(open)
            }
            if failures.isEmpty {
                status = .done("Added \(created) task\(created == 1 ? "" : "s") to the planner.")
            } else {
                status = .problem("Added \(created); \(failures.count) failed. "
                                  + failures.joined(separator: " "))
            }
        }
    }

    private func consumePrefill() {
        let text = AnywhereController.shared.pendingPrefill
        guard let text, !text.isEmpty else { return }
        AnywhereController.shared.pendingPrefill = nil
        // The palette only *prefills*; the person still presses Ask, and the
        // question is about the selected document.
        question = text
    }

    // MARK: - Save

    private var savingDocument: ResultExport {
        ResultExport(text: savingResult?.text ?? "")
    }

    private func exportName(_ result: DocumentResult) -> String {
        let base = (result.sourceName as NSString).deletingPathExtension
        let name = PreferenceRuntime.shared.store.generatedFileName(baseName: base)
        return name.contains(".") ? (name as NSString).deletingPathExtension : name
    }

    private func byteLabel(_ bytes: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }

    private func formatNumber(_ value: Int) -> String {
        NumberFormatter.localizedString(from: NSNumber(value: value), number: .decimal)
    }
}

/// A text result, exported through SwiftUI's own save panel.
///
/// `FileDocument` rather than writing directly: the person chooses the
/// destination, which is what the brief requires for a generated summary, and
/// macOS's panel refuses to overwrite silently.
struct ResultExport: FileDocument {
    static var readableContentTypes: [UTType] { [.plainText] }
    var text: String

    init(text: String) { self.text = text }

    init(configuration: ReadConfiguration) throws {
        text = configuration.file.regularFileContents
            .flatMap { String(data: $0, encoding: .utf8) } ?? ""
    }

    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: Data(text.utf8))
    }
}