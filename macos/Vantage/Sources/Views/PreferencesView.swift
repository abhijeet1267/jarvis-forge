import SwiftUI

/// Preferences — the small, explicit set of things Vantage remembers.
///
/// The screen is built around the brief's constraints:
/// * every row says **what it changes**, with the use site named;
/// * a conflicting pair is shown as a conflict, with the rule that applied;
/// * a value that looks like a credential or a chunk of document text is refused
///   at entry, with the refusal shown on the field;
/// * "Restore safe defaults" is a destructive action behind a confirmation.
struct PreferencesView: View {

    @ObservedObject private var store = PreferenceRuntime.shared.store

    @State private var addingKind: Preference.Kind = .preferredApplication
    @State private var newKey = ""
    @State private var newValue = ""
    @State private var newNote = ""
    @State private var formError: String?
    @State private var status: Status = .idle
    @State private var editing: UUID?
    @State private var editValue = ""
    @State private var editNote = ""
    @State private var confirmingReset = false
    @State private var pendingDelete: Preference?

    private enum Status: Equatable {
        case idle
        case done(String)
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
                    statusLine
                    if !store.conflicts.isEmpty { conflictsCard }
                    addCard
                    listCard
                    explainer
                }
                .padding(.horizontal, V.Space.gutter)
                .padding(.bottom, V.Space.section)
                .frame(maxWidth: 780, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .background(V.background)
        .vConfirm("Restore safe defaults?",
                  message: "Every saved preference will be deleted. Vantage will go back to asking "
                    + "you each time. This cannot be undone.",
                  confirm: "Delete all preferences",
                  isPresented: $confirmingReset) {
            switch store.removeAll() {
            case .success: status = .done("All preferences removed.")
            case .failure(let error): status = .problem(error.localizedDescription)
            }
        }
        .vConfirm("Delete this preference?",
                  message: pendingDelete.map { "“\($0.kind.displayName): \($0.key)” will be removed." } ?? "",
                  confirm: "Delete",
                  isPresented: Binding(get: { pendingDelete != nil },
                                       set: { if !$0 { pendingDelete = nil } })) {
            if let preference = pendingDelete {
                switch store.delete(preference.id) {
                case .success: status = .done("Deleted.")
                case .failure(let error): status = .problem(error.localizedDescription)
                }
            }
            pendingDelete = nil
        }
    }

    // MARK: - Header

    private var header: some View {
        PageHeader(title: "Preferences",
                   subtitle: "What Vantage remembers, because you told it to.") {
            HStack(spacing: V.Space.s) {
                Text("\(store.preferences.filter(\.enabled).count) active")
                    .font(V.Font.caption).foregroundStyle(V.textTertiary)
                Button("Restore defaults…") { confirmingReset = true }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(store.preferences.isEmpty || store.isReadOnly)
            }
        }
        .padding(.horizontal, V.Space.gutter)
        .padding(.top, V.Space.section)
        .padding(.bottom, V.Space.l)
        .frame(maxWidth: 780)
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

    // MARK: - Conflicts

    private var conflictsCard: some View {
        Card(tone: .warning) {
            VStack(alignment: .leading, spacing: V.Space.s) {
                HStack(spacing: V.Space.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(V.warning)
                    Text("Conflicting preferences")
                        .font(V.Font.sectionTitle).foregroundStyle(V.text)
                }
                ForEach(store.conflicts, id: \.key) { conflict in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("“\(conflict.key)” (\(conflict.kind.displayName))")
                            .font(V.Font.body).foregroundStyle(V.text)
                        Text("The most recently changed one wins: "
                             + (conflict.preferences.first.map {
                                 "“\($0.value)”" } ?? "")
                             + ". Change or disable one to remove the conflict.")
                            .font(V.Font.caption).foregroundStyle(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    // MARK: - Add

    private var addCard: some View {
        Card {
            VStack(alignment: .leading, spacing: V.Space.s) {
                SectionBlock(title: "Add a preference") {
                    VStack(alignment: .leading, spacing: V.Space.s) {
                        HStack(spacing: V.Space.s) {
                            Picker("", selection: $addingKind) {
                                ForEach(Preference.Kind.allCases, id: \.self) { kind in
                                    Label(kind.displayName, systemImage: kind.symbol).tag(kind)
                                }
                            }
                            .labelsHidden().frame(width: 200)
                            .onChange(of: addingKind) { _ in newKey = ""; newValue = "" }
                        }
                        Text(addingKind.valueHint)
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            .fixedSize(horizontal: false, vertical: true)
                        HStack(spacing: V.Space.s) {
                            VField(label: "Key", placeholder: keyPlaceholder, text: $newKey)
                            VField(label: "Value", placeholder: valuePlaceholder, text: $newValue)
                        }
                        VField(label: "Note (optional, why you set it)", placeholder: "",
                               text: $newNote)
                        if let formError {
                            Label(formError, systemImage: "exclamationmark.octagon.fill")
                                .font(V.Font.caption).foregroundStyle(V.danger)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        HStack {
                            Text("Preferences never store keys, tokens, passwords or document text.")
                                .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            Spacer()
                            Button("Add preference") { add() }
                                .buttonStyle(VPrimaryButtonStyle())
                                .disabled(store.isReadOnly
                                          || newKey.trimmingCharacters(in: .whitespaces).isEmpty
                                          || newValue.trimmingCharacters(in: .whitespaces).isEmpty)
                        }
                    }
                }
            }
        }
    }

    private var keyPlaceholder: String {
        switch addingKind {
        case .preferredApplication: return "browser, editor, terminal"
        case .preferredFolder: return "summary"
        case .fileNaming: return "pattern"
        case .summaryFormat: return "format"
        case .briefingSection: return "first"
        case .defaultFlowPolicy: return "policy"
        }
    }

    private var valuePlaceholder: String {
        switch addingKind {
        case .preferredApplication: return "Safari"
        case .preferredFolder: return "Downloads"
        case .fileNaming: return "{name} summary {date}"
        case .summaryFormat: return "brief"
        case .briefingSection: return "overdue"
        case .defaultFlowPolicy: return "stop"
        }
    }

    // MARK: - List

    private var listCard: some View {
        SectionBlock(title: "Saved preferences") {
            Card {
                if store.preferences.isEmpty {
                    VStack(alignment: .leading, spacing: V.Space.xs) {
                        Text("Nothing saved yet.")
                            .font(V.Font.body).foregroundStyle(V.textSecondary)
                        Text("Vantage only remembers what you add here. It does not infer "
                             + "preferences from conversations.")
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    VStack(spacing: 0) {
                        ForEach(store.preferences) { preference in
                            row(preference)
                            if preference.id != store.preferences.last?.id {
                                Divider().overlay(V.divider)
                            }
                        }
                    }
                }
            }
        }
    }

    private func row(_ preference: Preference) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(alignment: .top, spacing: V.Space.s) {
                Image(systemName: preference.kind.symbol)
                    .font(.system(size: 11))
                    .foregroundStyle(preference.enabled ? V.accent : V.textTertiary)
                    .frame(width: 16).padding(.top, 3)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: V.Space.xs) {
                        Text(preference.key)
                            .font(V.Font.label).foregroundStyle(V.text)
                        Text(preference.kind.displayName)
                            .font(V.Font.caption).foregroundStyle(V.textTertiary)
                    }
                    if editing == preference.id {
                        VStack(alignment: .leading, spacing: V.Space.xs) {
                            TextField("Value", text: $editValue)
                                .textFieldStyle(.roundedBorder)
                                .font(V.Font.body)
                            TextField("Note", text: $editNote)
                                .textFieldStyle(.roundedBorder)
                                .font(V.Font.body)
                            HStack(spacing: V.Space.s) {
                                Button("Cancel") { editing = nil }
                                    .buttonStyle(VQuietButtonStyle(tint: V.textSecondary))
                                Button("Save") { commitEdit(preference) }
                                    .buttonStyle(VPrimaryButtonStyle())
                            }
                        }
                    } else {
                        Text(preference.value)
                            .font(V.Font.body).foregroundStyle(V.text)
                            .textSelection(.enabled)
                        if !preference.note.isEmpty {
                            Text(preference.note)
                                .font(V.Font.caption).foregroundStyle(V.textTertiary)
                        }
                    }
                }
                Spacer(minLength: V.Space.s)
                Toggle("", isOn: Binding(
                    get: { preference.enabled },
                    set: { setEnabled($0, preference) }))
                    .labelsHidden().toggleStyle(.switch).tint(V.accent)
                    .help(preference.enabled ? "Disable this preference" : "Enable this preference")
                Menu {
                    Button("Edit…") {
                        editing = preference.id
                        editValue = preference.value
                        editNote = preference.note
                    }
                    Button(role: .destructive) { pendingDelete = preference } label: {
                        Text("Delete…")
                    }
                } label: { Image(systemName: "ellipsis.circle.fill") }
                .menuStyle(.borderlessButton)
                .frame(width: 24)
            }
        }
        .padding(.vertical, V.Space.s)
        .opacity(preference.enabled ? 1 : 0.6)
    }

    private var explainer: some View {
        Card(tone: .ivory) {
            VStack(alignment: .leading, spacing: V.Space.xs) {
                Text("How preferences are used")
                    .font(V.Font.label).foregroundStyle(V.text)
                Text("A preference supplies a *default* — where a summary is saved, how a new "
                     + "flow is set up, which section the briefing leads with. A preference never "
                     + "overrides something you ask for in the moment: asking Vantage to open "
                     + "Chrome opens Chrome even if your preferred browser is Safari. Every "
                     + "consequential action still goes through the same approval and risk "
                     + "checks.")
                    .font(V.Font.caption).foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - Actions

    private func add() {
        formError = nil
        let preference = Preference(kind: addingKind, key: newKey, value: newValue,
                                    note: newNote)
        switch store.add(preference) {
        case .success:
            newKey = ""; newValue = ""; newNote = ""
            status = .done("Saved.")
        case .failure(let error):
            formError = error.localizedDescription
        }
    }

    private func commitEdit(_ preference: Preference) {
        var updated = preference
        updated.value = editValue
        updated.note = editNote
        switch store.update(updated) {
        case .success:
            editing = nil
            status = .done("Saved your changes.")
        case .failure(let error):
            status = .problem(error.localizedDescription)
        }
    }

    private func setEnabled(_ enabled: Bool, _ preference: Preference) {
        var updated = preference
        updated.enabled = enabled
        switch store.update(updated) {
        case .success: break
        case .failure(let error): status = .problem(error.localizedDescription)
        }
    }
}