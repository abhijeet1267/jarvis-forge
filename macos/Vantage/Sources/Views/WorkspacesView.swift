import SwiftUI

/// Saved Workspaces: a named set of things opened together.
///
/// The screen is a list plus an editor. The editor is deliberately plain — a
/// name, a project folder, and three newline-separated lists — because a
/// workspace is a saved *reference*, not a workflow: it holds no logic that
/// could silently do something other than what is written here.
///
/// Launching reports every member separately. "Launched" as a single word would
/// hide the case this screen exists to show: three of four things opened, and
/// which one did not.
struct WorkspacesView: View {

    @State private var workspaces: [APIModels.Workspace] = []
    @State private var isLoading = true
    @State private var errorMessage: String?

    @State private var editing: APIModels.Workspace?
    @State private var draftName = ""
    @State private var draftDescription = ""
    @State private var draftDir = ""
    @State private var draftApps = ""
    @State private var draftFiles = ""
    @State private var draftURLs = ""
    @State private var saving = false
    @State private var editorError: String?

    @State private var launching: String?
    @State private var launch: APIModels.WorkspaceLaunchResult?

    @State private var pendingDelete: APIModels.Workspace?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Workspaces",
                           subtitle: "A project, its applications, its files, its links.")
                { Button("New workspace") { beginCreate() }
                    .buttonStyle(VPrimaryButtonStyle()) }

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await load() } })
                }

                if let launch { launchPanel(launch) }
                if editing != nil { editor }
                list
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
        .task { await load() }
        .alert("Delete this workspace?", isPresented: deleteBinding, presenting: pendingDelete) { workspace in
            Button("Delete", role: .destructive) { Task { await remove(workspace) } }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: { workspace in
            // Deleting a workspace removes the saved references only. Nothing on
            // disk and no running application is touched, and saying so prevents
            // the opposite assumption.
            Text("\"\(workspace.name)\" is removed from the list. No files are "
                 + "deleted and no applications are closed.")
        }
    }

    // MARK: - List

    private var list: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            SectionHeader("Saved")
            if isLoading {
                ProgressView().controlSize(.small)
            } else if workspaces.isEmpty {
                Text("No workspaces yet. Create one to open a project, its apps and "
                     + "its links with a single click.")
                    .font(V.Font.body)
                    .foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(workspaces) { workspace in
                row(workspace)
            }
        }
    }

    private func row(_ workspace: APIModels.Workspace) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(spacing: V.Space.s) {
                Text(workspace.name)
                    .font(V.Font.cardTitle).foregroundColor(V.text)
                Spacer()
                Button(launching == workspace.workspace_id ? "Opening…" : "Launch") {
                    Task { await run(workspace) }
                }
                .buttonStyle(VPrimaryButtonStyle())
                .disabled(launching != nil || workspace.isEmpty)
            }
            if !workspace.description.isEmpty {
                Text(workspace.description)
                    .font(V.Font.caption).foregroundColor(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !workspace.project_dir.isEmpty {
                Text(workspace.project_dir)
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
                    .lineLimit(1).truncationMode(.middle)
            }
            HStack(spacing: V.Space.m) {
                if !workspace.apps.isEmpty {
                    Text("\(workspace.apps.count) app(s)").font(V.Font.caption)
                        .foregroundColor(V.textTertiary)
                }
                if !workspace.files.isEmpty {
                    Text("\(workspace.files.count) file(s)").font(V.Font.caption)
                        .foregroundColor(V.textTertiary)
                }
                if !workspace.urls.isEmpty {
                    Text("\(workspace.urls.count) link(s)").font(V.Font.caption)
                        .foregroundColor(V.textTertiary)
                }
                Spacer()
                Text(launchHistory(workspace))
                    .font(V.Font.caption).foregroundColor(V.textTertiary)
            }
            HStack(spacing: V.Space.s) {
                Button("Edit") { beginEdit(workspace) }
                    .buttonStyle(VSecondaryButtonStyle())
                Button("Duplicate") { Task { await duplicate(workspace) } }
                    .buttonStyle(VSecondaryButtonStyle())
                Button("Delete") { pendingDelete = workspace }
                    .buttonStyle(VSecondaryButtonStyle())
                Spacer()
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func launchHistory(_ workspace: APIModels.Workspace) -> String {
        guard workspace.launch_count > 0 else { return "never launched" }
        let when = workspace.last_launched_at ?? "earlier"
        return "launched \(workspace.launch_count)× · last \(when)"
    }

    // MARK: - Editor

    private var editor: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            SectionHeader(editing == nil ? "New workspace" : "Edit workspace")

            TextField("Name", text: $draftName)
                .textFieldStyle(.roundedBorder).font(V.Font.body)
            TextField("Description (optional)", text: $draftDescription)
                .textFieldStyle(.roundedBorder).font(V.Font.body)

            HStack(spacing: V.Space.s) {
                TextField("Project folder", text: $draftDir)
                    .textFieldStyle(.roundedBorder).font(V.Font.mono)
                Button("Choose…") { chooseFolder() }
                    .buttonStyle(VSecondaryButtonStyle())
            }

            // Lists rather than a picker over the app registry: an unknown name is
            // refused at launch with its reason, which is more honest than a list
            // that silently cannot contain everything a person might want.
            memberEditor("Applications (one per line — e.g. finder, terminal)",
                         $draftApps)
            memberEditor("Files (one absolute path per line)", $draftFiles)
            memberEditor("Links (one http(s) URL per line)", $draftURLs)

            if let editorError {
                Text(editorError).font(V.Font.caption).foregroundColor(V.danger)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack(spacing: V.Space.s) {
                Button(saving ? "Saving…" : "Save") { Task { await save() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(saving || draftName.trimmingCharacters(in: .whitespaces).isEmpty)
                Button("Cancel") { cancelEdit() }
                    .buttonStyle(VSecondaryButtonStyle())
                Spacer()
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func memberEditor(_ title: String, _ text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xxs) {
            Text(title).font(V.Font.caption).foregroundColor(V.textTertiary)
            TextEditor(text: text)
                .font(V.Font.mono)
                .frame(minHeight: 52, maxHeight: 80)
                .padding(V.Space.xs)
                .background(V.surfaceAlt)
                .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
        }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use folder"
        if panel.runModal() == .OK, let url = panel.url {
            draftDir = url.path
        }
    }

    // MARK: - Launch result

    /// Every member's outcome, including the ones that did not open. The header
    /// says "verified" only when the backend said every member succeeded.
    private func launchPanel(_ result: APIModels.WorkspaceLaunchResult) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Last launch")
            HStack(spacing: V.Space.s) {
                Image(systemName: result.verified
                      ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .foregroundColor(result.verified ? V.success : V.warning)
                Text(result.summary)
                    .font(V.Font.body)
                    .foregroundColor(result.verified ? V.text : V.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(result.results.enumerated()), id: \.offset) { _, member in
                HStack(alignment: .top, spacing: V.Space.s) {
                    Image(systemName: member.ok ? "checkmark" : "xmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(member.ok ? V.success : V.danger)
                        .frame(width: 12)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(member.target)
                            .font(V.Font.mono).foregroundColor(V.text)
                            .lineLimit(1).truncationMode(.middle)
                        Text(member.detail)
                            .font(V.Font.caption).foregroundColor(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                }
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Actions

    private func beginCreate() {
        editing = nil
        draftName = ""
        draftDescription = ""
        draftDir = ""
        draftApps = ""
        draftFiles = ""
        draftURLs = ""
        editorError = nil
        launch = nil
    }

    private func beginEdit(_ workspace: APIModels.Workspace) {
        editing = workspace
        draftName = workspace.name
        draftDescription = workspace.description
        draftDir = workspace.project_dir
        draftApps = workspace.apps.joined(separator: "\n")
        draftFiles = workspace.files.joined(separator: "\n")
        draftURLs = workspace.urls.joined(separator: "\n")
        editorError = nil
        launch = nil
    }

    private func cancelEdit() {
        editing = nil
        editorError = nil
        draftName = ""
        draftDescription = ""
        draftDir = ""
        draftApps = ""
        draftFiles = ""
        draftURLs = ""
    }

    private func lines(_ text: String) -> [String] {
        text.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    private func save() async {
        saving = true
        editorError = nil
        defer { saving = false }
        let request = APIModels.WorkspaceRequest(
            name: draftName.trimmingCharacters(in: .whitespaces),
            description: draftDescription,
            project_dir: draftDir.trimmingCharacters(in: .whitespaces),
            apps: lines(draftApps), files: lines(draftFiles), urls: lines(draftURLs))
        do {
            if let existing = editing {
                _ = try await api.patch("/saved-workspaces/\(existing.workspace_id)",
                                        body: request,
                                        as: APIModels.Workspace.self)
            } else {
                _ = try await api.post("/saved-workspaces", body: request,
                                       as: APIModels.Workspace.self)
            }
            cancelEdit()
            await load()
        } catch {
            // A name collision is a 409 whose detail says which name; that is
            // more useful in the editor than in the page-level banner.
            editorError = error.localizedDescription
        }
    }

    private func run(_ workspace: APIModels.Workspace) async {
        launching = workspace.workspace_id
        errorMessage = nil
        defer { launching = nil }
        do {
            launch = try await api.post("/saved-workspaces/\(workspace.workspace_id)/launch",
                                        as: APIModels.WorkspaceLaunchResult.self)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func duplicate(_ workspace: APIModels.Workspace) async {
        errorMessage = nil
        do {
            _ = try await api.post("/saved-workspaces/\(workspace.workspace_id)/duplicate",
                                   body: APIModels.WorkspaceDuplicateRequest(name: nil),
                                   as: APIModels.Workspace.self)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func remove(_ workspace: APIModels.Workspace) async {
        pendingDelete = nil
        errorMessage = nil
        do {
            let _: APIModels.WorkspaceDeleteResult =
                try await api.delete("/saved-workspaces/\(workspace.workspace_id)")
            if editing?.workspace_id == workspace.workspace_id { cancelEdit() }
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private var deleteBinding: Binding<Bool> {
        Binding(get: { pendingDelete != nil },
                set: { if !$0 { pendingDelete = nil } })
    }

    private func load() async {
        errorMessage = nil
        do {
            let response = try await api.get("/saved-workspaces",
                                             as: APIModels.SavedWorkspaceList.self)
            workspaces = response.workspaces
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}