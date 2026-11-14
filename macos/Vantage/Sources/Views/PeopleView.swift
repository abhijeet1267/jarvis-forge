import SwiftUI

/// People and posts — the native counterpart of the operator page's
/// communications and professional-posts sections.
///
/// Two honest boundaries are shown, not hidden:
/// * a **call brief** is preparation. Vantage does not place the call, and the
///   card says so;
/// * a **post** can be drafted and scheduled. Publishing has no approved
///   integration, so the pane states that rather than offering a button that
///   cannot work.
struct PeopleView: View {

    @State private var contacts: [APIModels.Contact] = []
    @State private var brief: APIModels.CallBrief?
    @State private var briefFor = ""
    @State private var callNotes = ""
    @State private var callNoteText = ""
    @State private var outreach: APIModels.OutreachDraft?
    @State private var posts: [APIModels.PostDraft] = []
    @State private var publishingNote = ""
    @State private var newName = ""
    @State private var newAddress = ""
    @State private var newPost = ""
    @State private var recapNote = ""
    @State private var recapBusy = false
    @State private var isLoading = true
    @State private var errorMessage: String?
    /// The last *read* failed, so an empty list below is unknown, not empty.
    @State private var readFailed = false

    private let api = APIClient.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if let error = errorMessage {
                ErrorBanner(message: error, retry: { Task { await load() } })
                    .padding(.horizontal, 24).padding(.bottom, 8)
            }
            if isLoading {
                Spacer(); ProgressView().frame(maxWidth: .infinity); Spacer()
            } else {
                ScrollView {
                    HStack(alignment: .top, spacing: 18) {
                        VStack(alignment: .leading, spacing: 18) { contactsSection; briefSection }
                        VStack(alignment: .leading, spacing: 18) { postsSection }
                    }
                    .padding(24)
                }
            }
        }
        .task { await load() }
    }

    private var header: some View {
        PageHeader(title: "People", subtitle: "Contacts, call briefs and prepared posts.") {
            Button { Task { await load() } } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .buttonStyle(VSecondaryButtonStyle())
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.section)
        .padding(.bottom, V.Space.l)
    }

    private var contactsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Contacts").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            if contacts.isEmpty {
                Text(readFailed
                     ? "I could not read your contacts, so this is not an empty list."
                     : "No contacts yet.")
                    .foregroundStyle(V.textSecondary)
            } else {
                ForEach(contacts) { c in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(c.name)
                            Text(c.address).font(.caption2).foregroundStyle(V.textSecondary)
                        }
                        Spacer()
                        Button("Prepare call") { Task { await prepareCall(c.name) } }
                            .buttonStyle(.borderless)
                    }
                    .padding(.vertical, 3)
                    Divider()
                }
            }
            HStack(spacing: 8) {
                TextField("Contact name", text: $newName).textFieldStyle(.roundedBorder)
                TextField("email@example.com", text: $newAddress).textFieldStyle(.roundedBorder)
                Button("Add") { Task { await addContact() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty
                              || newAddress.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var briefSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Call brief").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            if let b = brief {
                Text(briefFor).font(.headline)
                if let addr = b.contact?.address, !addr.isEmpty {
                    Text(addr).font(.caption2).foregroundStyle(V.textSecondary)
                }
                ForEach(Array((b.talking_points ?? []).enumerated()), id: \.offset) { _, p in
                    Text("• \(p)").font(.caption)
                }
                ForEach(Array((b.open_tasks ?? []).enumerated()), id: \.offset) { _, t in
                    Text("task: \(t.title ?? "")").font(.caption2).foregroundStyle(V.textSecondary)
                }
                ForEach(Array((b.recent_email ?? []).enumerated()), id: \.offset) { _, m in
                    Text("email: \(m.subject ?? "")").font(.caption2).foregroundStyle(V.textSecondary)
                }
                if let e = b.email_error, !e.isEmpty {
                    Text("note: \(e)").font(.caption2).foregroundStyle(V.warning)
                }
                // Calling is available now, and it is deliberate: the button
                // dials FaceTime, and what is recorded afterwards is the
                // person's own account — Vantage cannot hear the call.
                HStack(spacing: 8) {
                    Button("Draft outreach") { Task { await draftOutreach() } }
                        .buttonStyle(.borderless)
                }
                if let o = outreach {
                    Text("Outreach draft (saved to mailbox drafts):")
                        .font(.caption2).foregroundStyle(V.textSecondary)
                    Text(o.draft?.body ?? "")
                        .font(.caption2)
                        .textSelection(.enabled)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: V.Radius.control)
                            .fill(V.fill))
                }
                TextField("What was said? Lines starting with - become tasks.",
                          text: $callNotes)
                    .textFieldStyle(.roundedBorder)
                    .font(.caption)
                Button("Save call notes") { Task { await saveCallNotes() } }
                    .buttonStyle(.borderless)
                if !callNoteText.isEmpty {
                    Text(callNoteText).font(.caption2).foregroundStyle(V.textSecondary)
                }
                Text("Vantage cannot see or hear the call, so it never claims one happened.")
                    .font(.caption2).foregroundStyle(V.textSecondary)
            } else {
                Text("Prepare a call to see a brief.").foregroundStyle(V.textSecondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(RoundedRectangle(cornerRadius: V.Radius.card).fill(V.fill))
    }

    private var postsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Post drafts").font(V.Font.caption.weight(.semibold)).tracking(0.6).foregroundStyle(V.textTertiary).textCase(.uppercase)
            if !publishingNote.isEmpty {
                Text(publishingNote).font(.caption2).foregroundStyle(V.warning)
            }
            if posts.isEmpty {
                Text(readFailed
                     ? "I could not read your drafts, so this is not an empty list."
                     : "No drafts yet.")
                    .foregroundStyle(V.textSecondary)
            } else {
                ForEach(posts) { d in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(d.status ?? "draft").font(.caption2).foregroundStyle(V.textSecondary)
                        Text(d.body)
                        HStack {
                            Spacer()
                            Button("Delete") { Task { await deletePost(d) } }.buttonStyle(.borderless)
                        }
                        .font(.caption)
                    }
                    .padding(.vertical, 4)
                    Divider()
                }
            }
            HStack(spacing: 8) {
                TextField("Draft a post…", text: $newPost).textFieldStyle(.roundedBorder)
                Button("Save") { Task { await savePost() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(newPost.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            HStack(spacing: 8) {
                Button("Draft a recap of this week") { Task { await draftRecap() } }
                    .disabled(recapBusy)
                if !recapNote.isEmpty {
                    Text(recapNote).font(.caption2).foregroundStyle(V.textSecondary)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true; errorMessage = nil; readFailed = false
        do {
            async let c = api.contacts()
            async let p = api.posts()
            // Read both before publishing either: assigning as each arrived
            // meant a failure on the second left a full contact list beside
            // "No drafts yet", under a banner saying nothing could be read.
            let (loadedContacts, response) = try await (c, p)
            contacts = loadedContacts
            posts = response.drafts ?? []
            publishingNote = response.publishing?.reason ?? ""
        } catch {
            errorMessage = error.localizedDescription
            readFailed = true
        }
        isLoading = false
    }

    private func addContact() async {
        let name = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        let address = newAddress.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, !address.isEmpty else { return }
        do {
            try await api.addContact(name: name, address: address)
            newName = ""; newAddress = ""
            await load()
        } catch { errorMessage = error.localizedDescription }
    }

    private func prepareCall(_ name: String) async {
        do {
            brief = try await api.prepareCall(name)
            briefFor = name
            outreach = nil
            callNoteText = ""
        } catch { errorMessage = error.localizedDescription }
    }

    private func draftOutreach() async {
        do {
            outreach = try await api.draftOutreach(briefFor)
        } catch { errorMessage = error.localizedDescription }
    }

    private func saveCallNotes() async {
        let text = callNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        do {
            let result = try await api.saveCallNotes(briefFor, notes: text)
            callNoteText = (result.created_count ?? 0) > 0
                ? "Created \(result.created_count!) task(s) from your notes."
                : (result.note ?? "Saved.")
            callNotes = ""
        } catch { errorMessage = error.localizedDescription }
    }

    private func savePost() async {
        let body = newPost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return }
        do {
            _ = try await api.createPost(body)
            newPost = ""
            await load()
        } catch { errorMessage = error.localizedDescription }
    }

    private func draftRecap() async {
        recapBusy = true
        do {
            let result = try await api.draftRecap()
            // Nothing completed is said plainly; no draft is invented.
            if let created = result.created {
                recapNote = "Drafted from \(result.completed_count ?? 0) completed task(s)."
                _ = created
                await load()
            } else {
                recapNote = result.reason ?? "Nothing to recap yet."
            }
        } catch {
            recapNote = error.localizedDescription
        }
        recapBusy = false
    }

    private func deletePost(_ d: APIModels.PostDraft) async {
        do {
            try await api.deletePost(d.draft_id)
            await load()
        } catch { errorMessage = error.localizedDescription }
    }
}