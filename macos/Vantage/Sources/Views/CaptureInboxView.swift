import SwiftUI
import AppKit

/// Quick Capture — a real, persistent inbox for things you want to keep.
///
/// The screen's structure follows the brief rather than a generic list: one
/// composer at the top, a filter row, and rows that say *what* an item is and
/// *where it has gone*. Conversion is a deliberate act with a reviewable draft —
/// a captured sentence never becomes a task or a flow because it looked like one.
struct CaptureInboxView: View {

    /// Set by another screen (a briefing card, the palette) before navigating
    /// here. Read once, then cleared.
    static var pendingFocus: UUID?

    @ObservedObject private var store = CaptureRuntime.shared.store
    @State private var draftText = ""
    @State private var draftKind: CaptureItem.Kind = .general
    /// The kind is chosen automatically from the words until the person picks one
    /// by hand; after that the picker's value wins.
    @State private var kindPickedManually = false
    @State private var search = ""
    @State private var filter: Filter = .open
    @State private var editing: UUID?
    @State private var editingText = ""
    @State private var editingKind: CaptureItem.Kind = .general
    @State private var editingTags = ""
    @State private var status: Status = .idle
    @State private var pendingDelete: CaptureItem?
    @State private var converting: CaptureItem?
    @State private var focus: UUID?
    @State private var isSaving = false

    private let conversion = CaptureConversionService()

    private enum Filter: String, CaseIterable {
        case open, inbox, all, archived, converted
        var title: String {
            switch self {
            case .open: return "Open"
            case .inbox: return "Needs review"
            case .all: return "All"
            case .archived: return "Archived"
            case .converted: return "Converted"
            }
        }
    }

    private enum Status: Equatable {
        case idle
        case done(String, Bool)
        case problem(String)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: V.Space.l) {
                    if let error = store.loadError {
                        Notice(kind: .error, message: error)
                    }
                    composer
                    filters
                    list
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.bottom, V.Space.section)
                .frame(maxWidth: 780, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .background(V.background)
        .task { consumeFocus() }
        .onReceive(AppNavigation.shared.$request) { _ in consumeFocus() }
        .vConfirm("Delete this item?", message: deleteMessage, confirm: "Delete",
                  isPresented: Binding(get: { pendingDelete != nil },
                                       set: { if !$0 { pendingDelete = nil } })) {
            if let item = pendingDelete { delete(item) }
            pendingDelete = nil
        }
        .sheet(item: $converting) { item in
            CaptureConversionSheet(item: item, conversion: conversion) { outcome in
                converting = nil
                if outcome.succeeded {
                    status = .done(outcome.summary, true)
                    // A drafted flow is handed to the Flows screen as an unsaved
                    // draft: review, then save. Nothing has run.
                    if let draft = outcome.flowDraft {
                        AppNavigation.shared.go(.flows, flowDraft: draft)
                    }
                } else {
                    status = .problem(outcome.error ?? "That could not be converted.")
                }
            }
        }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(title: "Quick Capture",
                   subtitle: "Things you wanted to keep, before they become anything.") {
            HStack(spacing: V.Space.s) {
                if !store.items.isEmpty {
                    Text("\(store.unreviewed.count) to review")
                        .font(V.Font.caption)
                        .foregroundStyle(V.textTertiary)
                }
                StatusPill(kind: store.isReadOnly ? .error
                            : (store.unreviewed.isEmpty ? .connected : .pending),
                           text: store.isReadOnly ? "Read-only"
                             : (store.unreviewed.isEmpty ? "Clear" : "\(store.unreviewed.count) waiting"))
            }
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.section)
        .padding(.bottom, V.Space.l)
        .frame(maxWidth: 780)
        .frame(maxWidth: .infinity, alignment: .center)
    }

    // MARK: - Composer

    private var composer: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(alignment: .top, spacing: V.Space.s) {
                    Image(systemName: draftKind.symbol)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(V.gold)
                        .padding(.top, 6)
                    TextField("Remember to…, capture this idea…, save this…",
                              text: $draftText, axis: .vertical)
                        .textFieldStyle(.plain)
                        .font(V.Font.message)
                        .lineLimit(1...5)
                        .onSubmit { save() }
                        .onChange(of: draftText) { newValue in
                            guard !kindPickedManually else { return }
                            // The words decide the kind until the person overrides
                            // it. An ambiguous sentence stays `.general` — which is
                            // exactly what "do not guess it is a task" requires.
                            draftKind = CaptureClassifier().classify(newValue).item?.kind ?? .general
                        }
                }
                HStack(spacing: V.Space.s) {
                    Picker("", selection: $draftKind) {
                        ForEach(CaptureItem.Kind.allCases, id: \.self) { kind in
                            Label(kind.displayName, systemImage: kind.symbol).tag(kind)
                        }
                    }
                    .labelsHidden()
                    .frame(width: 178)
                    .onChange(of: draftKind) { _ in kindPickedManually = true }

                    if let hint = captureHint {
                        Text(hint)
                            .font(V.Font.caption)
                            .foregroundStyle(V.textTertiary)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                    Spacer(minLength: V.Space.s)
                    if isSaving { ProgressView().controlSize(.small) }
                    Button("Capture") { save() }
                        .buttonStyle(VPrimaryButtonStyle())
                        .disabled(draftText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                  || store.isReadOnly)
                }
                statusLine
            }
        }
    }

    /// What the classifier thinks, said as a suggestion rather than a decision.
    private var captureHint: String? {
        guard !draftText.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        guard let item = CaptureClassifier().classify(draftText).item else {
            return "This will be saved as-is."
        }
        if let due = item.suggestedDue {
            return "Mentions \(due.formatted(date: .abbreviated, time: .shortened)) — you choose "
                + "whether it becomes a task."
        }
        return "Read as a \(item.kind.displayName.lowercased())."
    }

    @ViewBuilder
    private var statusLine: some View {
        switch status {
        case .idle:
            EmptyView()
        case .done(let message, let ok):
            Label(message, systemImage: ok ? "checkmark.circle.fill" : "questionmark.circle.fill")
                .font(V.Font.caption)
                .foregroundStyle(ok ? V.accent : V.warning)
                .fixedSize(horizontal: false, vertical: true)
        case .problem(let message):
            Label(message, systemImage: "exclamationmark.octagon.fill")
                .font(V.Font.caption)
                .foregroundStyle(V.danger)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - Filters

    private var filters: some View {
        HStack(spacing: V.Space.s) {
            Picker("", selection: $filter) {
                ForEach(Filter.allCases, id: \.self) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(maxWidth: 420)

            TextField("Search", text: $search)
                .textFieldStyle(.plain)
                .font(V.Font.body)
                .padding(.horizontal, V.Space.m)
                .padding(.vertical, 6)
                .background(V.surface, in: RoundedRectangle(cornerRadius: V.Radius.control))
                .overlay(RoundedRectangle(cornerRadius: V.Radius.control)
                    .strokeBorder(V.border, lineWidth: 1))
                .frame(maxWidth: 200)
        }
    }

    // MARK: - List

    private var visible: [CaptureItem] {
        var items = store.search(search)
        switch filter {
        case .open: items = items.filter(\.isOpen)
        case .inbox: items = items.filter { $0.status == .inbox }
        case .all: break
        case .archived: items = items.filter { $0.status == .archived }
        case .converted: items = items.filter { $0.status == .converted }
        }
        return items
    }

    @ViewBuilder
    private var list: some View {
        if store.items.isEmpty {
            EmptyState(symbol: "tray",
                       title: "Your inbox is empty",
                       message: "Capture a thought above, or say “capture this idea: …” from "
                         + "Vantage Anywhere. Everything you save is kept here until you decide "
                         + "what it is.")
        } else if visible.isEmpty {
            EmptyState(symbol: search.isEmpty ? "line.3.horizontal.decrease.circle" : "magnifyingglass",
                       title: search.isEmpty ? "Nothing in this filter" : "No matches",
                       message: search.isEmpty
                         ? "There are items in your inbox, but none with this status."
                         : "Nothing in your inbox matches “\(search)”.")
        } else {
            SectionBlock(title: "\(visible.count) item\(visible.count == 1 ? "" : "s")") {
                VStack(spacing: V.Space.s) {
                    ForEach(visible) { item in
                        row(item)
                    }
                }
            }
        }
    }

    private func row(_ item: CaptureItem) -> some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                if editing == item.id {
                    editor(item)
                } else {
                    readRow(item)
                }
            }
        }
        .overlay(
            RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                .strokeBorder(focus == item.id ? V.accent.opacity(0.6) : .clear, lineWidth: 1.5)
        )
    }

    private func readRow(_ item: CaptureItem) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(alignment: .top, spacing: V.Space.s) {
                Image(systemName: item.kind.symbol)
                    .font(.system(size: 12))
                    .foregroundStyle(V.textSecondary)
                    .frame(width: 16)
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.content)
                        .font(V.Font.body)
                        .foregroundStyle(V.text)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: V.Space.s) {
                        Text(item.kind.displayName)
                        Text("·")
                        Text(item.createdAt.formatted(date: .abbreviated, time: .shortened))
                        if let due = item.suggestedDue {
                            Text("·")
                            Label(due.formatted(date: .abbreviated, time: .omitted),
                                  systemImage: "calendar")
                        }
                        if !item.tags.isEmpty {
                            Text("·")
                            Text(item.tags.joined(separator: ", "))
                        }
                    }
                    .font(V.Font.caption)
                    .foregroundStyle(V.textTertiary)
                    if item.status == .converted, let label = item.destinationLabel {
                        Label(label, systemImage: "arrow.turn.down.right")
                            .font(V.Font.caption)
                            .foregroundStyle(V.accent)
                    }
                }
                Spacer(minLength: V.Space.s)
                statusPill(item)
            }
            actions(item)
        }
    }

    private func statusPill(_ item: CaptureItem) -> some View {
        Text(item.status.displayName)
            .font(V.Font.caption)
            .foregroundStyle(item.status == .inbox ? V.gold
                             : (item.status == .converted ? V.accent : V.textTertiary))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background((item.status == .inbox ? V.goldSoft
                        : (item.status == .converted ? V.accentSoft : V.fill)),
                        in: Capsule())
    }

    private func actions(_ item: CaptureItem) -> some View {
        HStack(spacing: V.Space.s) {
            if item.status == .inbox {
                Button("Reviewed") { setStatus(.reviewed, item) }
                    .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
            }
            Menu {
                if item.status != .converted {
                    Button {
                        converting = item
                    } label: { Label("Convert…", systemImage: "arrow.turn.down.right") }
                }
                Button {
                    beginEdit(item)
                } label: { Label("Edit", systemImage: "pencil") }
                Divider()
                if item.status == .archived {
                    Button {
                        setStatus(.reviewed, item)
                    } label: { Label("Restore", systemImage: "arrow.uturn.backward") }
                } else {
                    Button {
                        setStatus(.archived, item)
                    } label: { Label("Archive", systemImage: "archivebox") }
                }
                Button(role: .destructive) {
                    pendingDelete = item
                } label: { Label("Delete…", systemImage: "trash") }
            } label: {
                // `.fill` rather than the outline variant: the outline glyph
                // rendered as an illegible squiggle at this size in a render,
                // and `.fill` is what the rest of the app's row menus use.
                Image(systemName: "ellipsis.circle.fill")
            }
            .menuStyle(.borderlessButton)
            .frame(width: 24)
            .help("More actions for this item")
            Spacer(minLength: 0)
            if item.status != .converted,
               item.kind.suggestedDestination != nil {
                Button("Convert…") { converting = item }
                    .buttonStyle(VSecondaryButtonStyle())
            }
        }
    }

    private func editor(_ item: CaptureItem) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            TextEditor(text: $editingText)
                .font(V.Font.body)
                .frame(minHeight: 60)
                .padding(6)
                .background(V.surfaceAlt, in: RoundedRectangle(cornerRadius: V.Radius.control))
                .overlay(RoundedRectangle(cornerRadius: V.Radius.control)
                    .strokeBorder(V.border, lineWidth: 1))
            HStack(spacing: V.Space.s) {
                Picker("", selection: $editingKind) {
                    ForEach(CaptureItem.Kind.allCases, id: \.self) { kind in
                        Text(kind.displayName).tag(kind)
                    }
                }
                .labelsHidden().frame(width: 170)
                TextField("Tags, comma separated", text: $editingTags)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.body)
                Spacer(minLength: V.Space.s)
                Button("Cancel") { editing = nil }
                    .buttonStyle(VSecondaryButtonStyle())
                Button("Save") { commitEdit(item) }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(editingText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
    }

    // MARK: - Focus

    private func consumeFocus() {
        guard let id = Self.pendingFocus else { return }
        Self.pendingFocus = nil
        focus = id
        filter = .all
        search = ""
        if let item = store.item(id) { beginEdit(item) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) {
            if focus == id { V.animate { focus = nil } }
        }
    }

    // MARK: - Actions

    private func save() {
        let text = draftText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        isSaving = true
        defer { isSaving = false }
        // An explicit kind wins; otherwise the classifier's reading (which may be
        // `.general` for an ambiguous sentence).
        let classified = CaptureClassifier().classify(text).item
        var item = classified ?? CaptureItem(content: text, kind: draftKind)
        if kindPickedManually { item.kind = draftKind }
        switch store.add(item) {
        case .success(let saved):
            draftText = ""
            draftKind = .general
            kindPickedManually = false
            status = .done("Saved as \(saved.kind.displayName.lowercased()).", true)
            focus = saved.id
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) {
                if focus == saved.id { V.animate { focus = nil } }
            }
        case .failure(let error):
            status = .problem(error.localizedDescription)
        }
    }

    private func beginEdit(_ item: CaptureItem) {
        editing = item.id
        editingText = item.content
        editingKind = item.kind
        editingTags = item.tags.joined(separator: ", ")
    }

    private func commitEdit(_ item: CaptureItem) {
        var updated = item
        updated.content = editingText
        updated.kind = editingKind
        updated.tags = editingTags
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        switch store.update(updated) {
        case .success:
            editing = nil
            status = .done("Saved your changes.", true)
        case .failure(let error):
            status = .problem(error.localizedDescription)
        }
    }

    private func setStatus(_ status: CaptureItem.Status, _ item: CaptureItem) {
        switch store.setStatus(status, for: item.id) {
        case .success:
            self.status = .done("Marked \(status.displayName.lowercased()).", true)
        case .failure(let error):
            self.status = .problem(error.localizedDescription)
        }
    }

    private func delete(_ item: CaptureItem) {
        switch store.delete(item.id) {
        case .success:
            status = .done("Deleted.", true)
        case .failure(let error):
            status = .problem(error.localizedDescription)
        }
    }

    private var deleteMessage: String {
        guard let item = pendingDelete else { return "" }
        let converted = item.status == .converted
            ? " The \(item.destinationLabel ?? "converted item") it points at is not affected."
            : ""
        return "“\(item.headline)” will be removed from your inbox permanently.\(converted)"
    }
}