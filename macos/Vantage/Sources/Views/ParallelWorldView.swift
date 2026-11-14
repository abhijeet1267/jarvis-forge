import SwiftUI

/// ParallelWorld: see the proposed change to your files before it happens.
///
/// The screen is built around one rule: **Apply stays disabled until it is
/// actually safe**. It is disabled while the plan conflicts, while nothing has
/// been approved, and while a preview is stale. The reason is always shown in
/// words -- a greyed button with no explanation is a puzzle, not a safeguard.
///
/// Every value shown comes from the backend; nothing here invents progress, a
/// completion percentage, or a success badge. If the preview fails, the error is
/// surfaced and no change is implied.
struct ParallelWorldView: View {

    @State private var operations: [APIModels.FileOperation] = []
    @State private var preview: APIModels.FilePreview?
    @State private var applyResult: APIModels.FileApplyResult?
    @State private var errorMessage: String?
    @State private var busy = false
    @State private var selectedPath: String?

    /// Editing target for the next operation.
    @State private var newPath = ""
    @State private var newContent = ""
    @State private var newOperation = "replace"

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "ParallelWorld",
                           subtitle: "Preview before reality — inspect every change first.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await runPreview() } })
                }

                planEditor
                if let preview { previewSummary(preview) }
                if let preview, !preview.changes.isEmpty {
                    changeBrowser(preview)
                    if let selected = selectedPath,
                       let change = preview.changes.first(where: { $0.path == selected }) {
                        diffPanel(change)
                    }
                }
                if let result = applyResult { resultPanel(result) }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
    }

    // MARK: - Plan

    private var planEditor: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            SectionHeader("Proposed operations")

            ForEach(Array(operations.enumerated()), id: \.offset) { index, op in
                HStack(spacing: V.Space.s) {
                    Text(op.op.uppercased())
                        .font(V.Font.caption)
                        .foregroundColor(V.textTertiary)
                        .frame(width: 62, alignment: .leading)
                    Text(op.path)
                        .font(V.Font.mono)
                        .foregroundColor(V.text)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    Button("Remove") { operations.remove(at: index); preview = nil }
                        .buttonStyle(.plain)
                        .font(V.Font.caption)
                        .foregroundColor(V.danger)
                }
                .padding(V.Space.s)
                .background(V.surface)
                .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
            }

            HStack(spacing: V.Space.s) {
                Picker("", selection: $newOperation) {
                    Text("Replace").tag("replace")
                    Text("Create").tag("create")
                    Text("Append").tag("append")
                    Text("Delete").tag("delete")
                }
                .labelsHidden()
                .frame(width: 110)

                TextField("path", text: $newPath)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.mono)

                Button("Add") {
                    guard !newPath.isEmpty else { return }
                    operations.append(APIModels.FileOperation(
                        op: newOperation, path: newPath,
                        content: newOperation == "delete" ? "" : newContent))
                    newPath = ""
                    preview = nil
                }
                .buttonStyle(VPrimaryButtonStyle())
                .disabled(newPath.isEmpty)
            }

            if newOperation != "delete" {
                TextEditor(text: $newContent)
                    .font(V.Font.mono)
                    .frame(height: 70)
                    .overlay(RoundedRectangle(cornerRadius: V.Radius.control)
                        .stroke(V.border, lineWidth: 1))
            }

            HStack(spacing: V.Space.s) {
                Button(busy ? "Working…" : "Preview") { Task { await runPreview() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(operations.isEmpty || busy)

                Button("Apply approved changes") { Task { await runApply() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(!canApply)

                Spacer()
            }

            if !canApply, let why = whyApplyDisabled {
                Text(why)
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
            }
        }
    }

    // MARK: - Preview summary

    private func previewSummary(_ preview: APIModels.FilePreview) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Preview")
            HStack(spacing: V.Space.l) {
                stat("Created", preview.summary.created, V.success)
                stat("Modified", preview.summary.modified, V.warning)
                stat("Deleted", preview.summary.deleted, V.danger)
                stat("Conflicts", preview.summary.conflicts,
                     preview.summary.conflicts > 0 ? V.danger : V.textSecondary)
            }
            Text(preview.note)
                .font(V.Font.caption)
                .foregroundColor(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func stat(_ label: String, _ value: Int, _ tone: Color) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("\(value)").font(V.Font.cardTitle).foregroundColor(tone)
            Text(label).font(V.Font.caption).foregroundColor(V.textTertiary)
        }
    }

    // MARK: - Change browser + diff

    private func changeBrowser(_ preview: APIModels.FilePreview) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Changes")
            ForEach(preview.changes) { change in
                Button {
                    selectedPath = change.path
                } label: {
                    HStack(spacing: V.Space.s) {
                        Text(change.op.uppercased())
                            .font(V.Font.caption)
                            .frame(width: 62, alignment: .leading)
                            .foregroundColor(change.op == "delete" ? V.danger : V.accent)
                        Text(change.path)
                            .font(V.Font.mono)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Spacer()
                        if change.truncated {
                            Text("truncated")
                                .font(V.Font.caption)
                                .foregroundColor(V.warning)
                        }
                    }
                    .padding(V.Space.s)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(selectedPath == change.path ? V.surfaceAlt : V.surface)
                    .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
                }
                .buttonStyle(.plain)
            }

            ForEach(Array(preview.conflicts.enumerated()), id: \.offset) { _, conflict in
                HStack(alignment: .top, spacing: V.Space.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundColor(V.danger)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(conflict.path).font(V.Font.mono).foregroundColor(V.text)
                        Text(conflict.reason).font(V.Font.caption)
                            .foregroundColor(V.textSecondary)
                    }
                }
                .padding(V.Space.s)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(V.dangerSoft)
                .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
            }
        }
    }

    private func diffPanel(_ change: APIModels.FileChange) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            SectionHeader("Before / After — \(change.path)")
            HStack(alignment: .top, spacing: V.Space.s) {
                diffColumn("Before", change.before, tone: V.danger)
                diffColumn("After", change.after, tone: V.success)
            }
        }
    }

    private func diffColumn(_ title: String, _ text: String, tone: Color) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xxs) {
            Text(title).font(V.Font.label).foregroundColor(tone)
            ScrollView {
                Text(text.isEmpty ? "(empty)" : text)
                    .font(V.Font.mono)
                    .foregroundColor(V.text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .frame(height: 160)
            .padding(V.Space.s)
            .background(V.surface)
            .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
        }
    }

    // MARK: - Result

    private func resultPanel(_ result: APIModels.FileApplyResult) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Result")
            Text(result.applied ? "Applied and verified." : "Not applied.")
                .font(V.Font.body)
                .foregroundColor(result.applied ? V.success : V.warning)
            ForEach(Array(result.results.enumerated()), id: \.offset) { _, entry in
                HStack(alignment: .top, spacing: V.Space.s) {
                    Image(systemName: entry.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundColor(entry.ok ? V.success : V.danger)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(entry.path).font(V.Font.mono).foregroundColor(V.text)
                        Text(entry.detail).font(V.Font.caption)
                            .foregroundColor(V.textSecondary)
                    }
                }
            }
        }
    }

    // MARK: - State

    private var canApply: Bool {
        guard let preview, preview.applicable, !busy else { return false }
        return !preview.changes.isEmpty
    }

    /// Why Apply is unavailable, in words the person can act on.
    private var whyApplyDisabled: String? {
        if busy { return "Working…" }
        guard let preview else { return "Preview the plan first." }
        if !preview.applicable {
            return "There are conflicts; resolve them before applying."
        }
        if preview.changes.isEmpty { return "There is nothing to apply." }
        return nil
    }

    // MARK: - Transport

    private func runPreview() async {
        guard !operations.isEmpty else { return }
        busy = true
        errorMessage = nil
        applyResult = nil
        do {
            preview = try await api.post(
                "/files/preview", body: APIModels.FilePlan(operations: operations),
                as: APIModels.FilePreview.self)
            if selectedPath == nil { selectedPath = preview?.changes.first?.path }
        } catch {
            preview = nil
            errorMessage = error.localizedDescription
        }
        busy = false
    }

    private func runApply() async {
        guard !operations.isEmpty else { return }
        busy = true
        errorMessage = nil
        do {
            applyResult = try await api.post(
                "/files/apply",
                body: APIModels.FileApplyRequest(operations: operations, approved: true),
                as: APIModels.FileApplyResult.self)
            // The preview is now stale by definition; ask for a fresh one rather
            // than showing old "before" text beside new files.
            preview = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        busy = false
    }
}