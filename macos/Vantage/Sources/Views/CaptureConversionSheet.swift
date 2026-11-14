import SwiftUI

/// Review before anything is created.
///
/// This sheet exists because the brief requires it: a captured sentence can
/// *suggest* a task, a reminder or a flow, but the person edits the actual draft
/// and presses a button before it exists. The sheet also names the consequence —
/// where the item will go and that the inbox entry becomes a pointer, not a copy.
struct CaptureConversionSheet: View {
    let item: CaptureItem
    let conversion: CaptureConversionService
    let onFinish: (CaptureConversionService.Outcome) -> Void

    @Environment(\.dismiss) private var dismiss

    @State private var destination: CaptureDestination
    @State private var title: String
    @State private var detail: String
    @State private var priority: String
    @State private var hasDueDate: Bool
    @State private var dueDate: Date
    @State private var isWorking = false
    @State private var error: String?

    init(item: CaptureItem, conversion: CaptureConversionService,
         onFinish: @escaping (CaptureConversionService.Outcome) -> Void) {
        self.item = item
        self.conversion = conversion
        self.onFinish = onFinish
        // The kind's own suggestion is the default destination; the person can
        // change it. A general item defaults to a task only if it named a date;
        // otherwise it defaults to keeping it as a note.
        _destination = State(initialValue: item.kind.suggestedDestination
                             ?? (item.suggestedDue != nil ? .task : .note))
        _title = State(initialValue: item.headline)
        _detail = State(initialValue: item.content)
        _priority = State(initialValue: "normal")
        _hasDueDate = State(initialValue: item.suggestedDue != nil)
        _dueDate = State(initialValue: item.suggestedDue
                         ?? Calendar.current.date(byAdding: .day, value: 1, to: Date())
                         ?? Date())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.l) {
            VStack(alignment: .leading, spacing: V.Space.xs) {
                Text("Convert this capture")
                    .font(V.Font.cardTitle).foregroundStyle(V.text)
                Text("Nothing is created until you press the button. You can edit everything here.")
                    .font(V.Font.caption).foregroundStyle(V.textSecondary)
            }

            Picker("", selection: $destination) {
                ForEach(CaptureDestination.allCases, id: \.self) { destination in
                    Text(destination.title).tag(destination)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            Text(destination.detail)
                .font(V.Font.caption)
                .foregroundStyle(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)

            switch destination {
            case .task, .reminder:
                fieldsForTaskOrReminder
            case .flow:
                fieldsForFlow
            case .note:
                Text("The item stays in your inbox, marked reviewed. Nothing is created.")
                    .font(V.Font.body)
                    .foregroundStyle(V.textSecondary)
            }

            if let error {
                Notice(kind: .error, message: error)
            }

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(isWorking)
                Button(buttonTitle) { perform() }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(isWorking || title.trimmingCharacters(in: .whitespaces).isEmpty)
                if isWorking { ProgressView().controlSize(.small) }
            }
        }
        .padding(V.Space.xl)
        .frame(width: 520)
        .background(V.background)
    }

    private var buttonTitle: String {
        switch destination {
        case .task: return "Create task"
        case .reminder: return "Set reminder"
        case .flow: return "Draft flow"
        case .note: return "Keep as reviewed"
        }
    }

    // MARK: - Task / reminder fields

    private var fieldsForTaskOrReminder: some View {
        VStack(alignment: .leading, spacing: V.Space.m) {
            VField(label: destination == .task ? "Task title" : "Reminder message",
                   placeholder: "What needs doing", text: $title)
            if destination == .task {
                VStack(alignment: .leading, spacing: V.Space.xs) {
                    Text("Detail").font(V.Font.label).foregroundStyle(V.textSecondary)
                    TextEditor(text: $detail)
                        .font(V.Font.body)
                        .frame(height: 64)
                        .padding(6)
                        .background(V.surface, in: RoundedRectangle(cornerRadius: V.Radius.control))
                        .overlay(RoundedRectangle(cornerRadius: V.Radius.control)
                            .strokeBorder(V.border, lineWidth: 1))
                }
                HStack(spacing: V.Space.m) {
                    Picker("Priority", selection: $priority) {
                        Text("Low").tag("low")
                        Text("Normal").tag("normal")
                        Text("High").tag("high")
                        Text("Urgent").tag("urgent")
                    }
                    .frame(maxWidth: 200)
                    Spacer()
                }
            }
            Toggle("Set a date", isOn: $hasDueDate)
                .toggleStyle(.checkbox)
            if hasDueDate {
                DatePicker("", selection: $dueDate,
                           displayedComponents: destination == .reminder
                             ? [.hourAndMinute] : [.date, .hourAndMinute])
                    .labelsHidden()
                if item.suggestedDue != nil {
                    Text("The date came from the wording of your capture. Check it before using it.")
                        .font(V.Font.caption).foregroundStyle(V.textTertiary)
                }
                if destination == .reminder {
                    // The scheduler can only express a clock time today or a
                    // short relative delay. Saying so here means the person sees
                    // the limit *before* pressing the button.
                    if let reason = reminderLimit(for: dueDate) {
                        Label(reason, systemImage: "exclamationmark.triangle.fill")
                            .font(V.Font.caption).foregroundStyle(V.warning)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private var fieldsForFlow: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("“\(item.content)”")
                .font(V.Font.body)
                .foregroundStyle(V.text)
                .fixedSize(horizontal: false, vertical: true)
            Text("Vantage will build an editable flow draft from those words. The draft opens in "
                 + "Flows, unsaved — you review it, then Save, then Run. Nothing runs from here.")
                .font(V.Font.caption)
                .foregroundStyle(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The reason a chosen reminder time cannot be expressed, or nil.
    ///
    /// Mirrors the client's own rule, so the sheet refuses before the request.
    private func reminderLimit(for date: Date) -> String? {
        let seconds = date.timeIntervalSinceNow
        if seconds <= 0 { return "That time is already past." }
        if seconds <= 90 * 60 { return nil }
        if Calendar.current.isDateInToday(date) { return nil }
        return "The scheduler can only set a reminder for a clock time today or a delay of "
            + "about an hour. This time can't be expressed — use a task's due date instead."
    }

    // MARK: - Perform

    private func perform() {
        error = nil
        switch destination {
        case .task:
            isWorking = true
            Task {
                let outcome = await conversion.convertToTask(
                    item, title: title, detail: detail, priority: priority,
                    due: hasDueDate ? dueDate : nil)
                isWorking = false
                finish(outcome)
            }
        case .reminder:
            isWorking = true
            Task {
                let outcome = await conversion.convertToReminder(
                    item, message: title, at: dueDate)
                isWorking = false
                finish(outcome)
            }
        case .flow:
            let outcome = conversion.convertToFlow(
                item, applications: MacControlCoordinator.shared.installedApplications)
            finish(outcome)
        case .note:
            switch CaptureRuntime.shared.store.setStatus(.reviewed, for: item.id) {
            case .success:
                finish(.init(summary: "Kept in your inbox, marked reviewed.", succeeded: true))
            case .failure(let error):
                self.error = error.localizedDescription
            }
        }
    }

    private func finish(_ outcome: CaptureConversionService.Outcome) {
        if outcome.succeeded {
            onFinish(outcome)
            dismiss()
        } else {
            error = outcome.error ?? "That could not be converted."
        }
    }
}