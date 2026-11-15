import SwiftUI

/// Smart Files: look inside a folder before changing anything.
///
/// The screen reports what is actually there — duplicate groups with the space
/// they waste, the largest files, anything that could not be read — and proposes
/// an organisation plan. Nothing here writes: a plan is handed to ParallelWorld,
/// where it is previewed and approved like any other file change. That is why
/// there is no "delete" button on this screen: removing duplicates is a
/// ParallelWorld operation, not a Smart Files one.
struct SmartFilesView: View {

    @State private var folder = ""
    @State private var report: APIModels.FolderScan?
    @State private var plan: APIModels.OrganizePlan?
    @State private var scanning = false
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Smart Files",
                           subtitle: "What is in a folder, before anything changes.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await scanNow() } })
                }

                chooser
                if let report { summary(report); duplicates(report); largest(report) }
                if let plan { planPanel(plan) }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
    }

    private var chooser: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(spacing: V.Space.s) {
                TextField("Folder to inspect", text: $folder)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.mono)
                Button("Choose…") { chooseFolder() }
                    .buttonStyle(VPrimaryButtonStyle())
                Button(scanning ? "Scanning…" : "Scan") { Task { await scanNow() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(folder.isEmpty || scanning)
            }
            if report != nil {
                HStack(spacing: V.Space.s) {
                    Button("Propose an organisation") { Task { await proposeOrganize() } }
                        .buttonStyle(VPrimaryButtonStyle())
                    Spacer()
                }
            }
        }
    }

    /// The native folder picker. No assumed filesystem access.
    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Scan"
        if panel.runModal() == .OK, let url = panel.url {
            folder = url.path
            Task { await scanNow() }
        }
    }

    // MARK: - Report

    private func summary(_ report: APIModels.FolderScan) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Summary")
            HStack(spacing: V.Space.l) {
                stat("Files", "\(report.file_count)", V.text)
                stat("Duplicate groups", "\(report.duplicate_groups.count)",
                     report.duplicate_groups.isEmpty ? V.success : V.warning)
                stat("Reclaimable", bytes(report.reclaimable_bytes), V.warning)
            }
            if report.truncated {
                Text("The scan hit its file limit, so this covers part of the folder. "
                     + "Pick a narrower folder for a complete answer.")
                    .font(V.Font.caption)
                    .foregroundColor(V.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(report.note)
                .font(V.Font.caption)
                .foregroundColor(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func duplicates(_ report: APIModels.FolderScan) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            if !report.duplicate_groups.isEmpty {
                SectionHeader("Duplicates")
                ForEach(Array(report.duplicate_groups.enumerated()), id: \.offset) { _, group in
                    VStack(alignment: .leading, spacing: V.Space.xxs) {
                        HStack {
                            Text("\(group.copies.count + 1) copies · \(bytes(group.size)) each")
                                .font(V.Font.label).foregroundColor(V.text)
                            Spacer()
                            Text("\(bytes(group.wasted_bytes)) wasted")
                                .font(V.Font.caption).foregroundColor(V.warning)
                        }
                        Text("keep  \(group.keep)")
                            .font(V.Font.mono).foregroundColor(V.success)
                            .lineLimit(1).truncationMode(.middle)
                        ForEach(Array(group.copies.enumerated()), id: \.offset) { _, copy in
                            Text("copy  \(copy)")
                                .font(V.Font.mono).foregroundColor(V.textSecondary)
                                .lineLimit(1).truncationMode(.middle)
                        }
                    }
                    .padding(V.Space.s)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(V.surface)
                    .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
                }
            }
            if !report.unreadable.isEmpty {
                Notice(kind: .error,
                       message: "\(report.unreadable.count) file(s) could not be read, "
                              + "so they were not checked for duplicates.",
                       actionTitle: "")
            }
        }
    }

    private func largest(_ report: APIModels.FolderScan) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Largest files")
            ForEach(Array(report.largest.enumerated()), id: \.offset) { _, entry in
                HStack(spacing: V.Space.s) {
                    Text(bytes(entry.size))
                        .font(V.Font.mono).foregroundColor(V.textSecondary)
                        .frame(width: 76, alignment: .leading)
                    Text(entry.path)
                        .font(V.Font.mono).foregroundColor(V.text)
                        .lineLimit(1).truncationMode(.middle)
                    Spacer()
                }
            }
        }
    }

    private func planPanel(_ plan: APIModels.OrganizePlan) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Organisation plan")
            Text(plan.summary)
                .font(V.Font.body)
                .foregroundColor(plan.applicable ? V.text : V.textTertiary)
            ForEach(Array(plan.operations.prefix(25).enumerated()), id: \.offset) { _, op in
                Text("\(op.path ?? "") → \(op.destination ?? "")")
                    .font(V.Font.mono)
                    .foregroundColor(V.textSecondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            Text("Nothing has moved. The plan goes to ParallelWorld, where you preview "
                 + "and approve it — this screen does not write.")
                .font(V.Font.caption)
                .foregroundColor(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func stat(_ label: String, _ value: String, _ tone: Color) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(value).font(V.Font.cardTitle).foregroundColor(tone)
            Text(label).font(V.Font.caption).foregroundColor(V.textTertiary)
        }
    }

    private func bytes(_ count: Int) -> String {
        let units = ["B", "KB", "MB", "GB"]
        var value = Double(count)
        var unit = 0
        while value >= 1024 && unit < units.count - 1 { value /= 1024; unit += 1 }
        return unit == 0 ? "\(count) B" : String(format: "%.1f %@", value, units[unit])
    }

    // MARK: - Transport

    private func scanNow() async {
        guard !folder.isEmpty else { return }
        scanning = true
        errorMessage = nil
        plan = nil
        do {
            report = try await api.post("/files/scan",
                                        body: APIModels.FolderScanRequest(path: folder),
                                        as: APIModels.FolderScan.self)
        } catch {
            errorMessage = error.localizedDescription
            report = nil
        }
        scanning = false
    }

    private func proposeOrganize() async {
        guard !folder.isEmpty else { return }
        errorMessage = nil
        do {
            plan = try await api.post("/files/organize-plan",
                                      body: APIModels.FolderScanRequest(path: folder),
                                      as: APIModels.OrganizePlan.self)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}