import SwiftUI

/// Project Doctor: pick a code project, see what is actually wrong with it.
///
/// Every finding on this screen names the evidence behind it — the check that
/// ran, the command it used, and its exit code. A check the backend could not
/// perform shows as "not checked", never as passing. Inspection reads files only;
/// running the project's own build or tests is a separate, explicit button,
/// because that is a real execution rather than a read.
struct ProjectDoctorView: View {

    @State private var path = ""
    @State private var report: APIModels.ProjectReport?
    @State private var repair: APIModels.ProjectRepair?
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var runningChecks = false

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Project Doctor",
                           subtitle: "What is actually wrong with a project, and why.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await inspect() } })
                }

                chooser
                if let report {
                    overview(report)
                    findings(report)
                    if let repair { repairPanel(repair) }
                }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
    }

    // MARK: - Chooser

    private var chooser: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(spacing: V.Space.s) {
                TextField("Path to a project directory", text: $path)
                    .textFieldStyle(.roundedBorder)
                    .font(V.Font.mono)

                Button("Choose…") { chooseFolder() }
                    .buttonStyle(VPrimaryButtonStyle())

                Button(isLoading ? "Inspecting…" : "Inspect") {
                    Task { await inspect() }
                }
                .buttonStyle(VPrimaryButtonStyle())
                .disabled(path.isEmpty || isLoading)
            }

            if report != nil {
                HStack(spacing: V.Space.s) {
                    Button(runningChecks ? "Running checks…" : "Run its build/tests") {
                        Task { await inspect(runChecks: true) }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(runningChecks || isLoading)

                    Button("Propose a repair") { Task { await proposeRepair() } }
                        .buttonStyle(VPrimaryButtonStyle())

                    Spacer()
                }
                Text("Reading a project is safe. Running its build or tests executes the "
                     + "project's own command, so it is a separate button.")
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// The native folder picker. No assumed filesystem access.
    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Inspect"
        if panel.runModal() == .OK, let url = panel.url {
            path = url.path
            Task { await inspect() }
        }
    }

    // MARK: - Overview

    private func overview(_ report: APIModels.ProjectReport) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Project")
            HStack(spacing: V.Space.l) {
                stat("Type", report.kind)
                stat("Manifest", report.manifest.isEmpty ? "—" : report.manifest)
                stat("Problems", "\(report.problems.count)")
            }
            Text(report.root)
                .font(V.Font.mono)
                .foregroundColor(V.textTertiary)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(value)
                .font(V.Font.cardTitle)
                .foregroundColor(value == "0" ? V.success : V.text)
            Text(label).font(V.Font.caption).foregroundColor(V.textTertiary)
        }
    }

    // MARK: - Findings

    private func findings(_ report: APIModels.ProjectReport) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Findings")
            ForEach(Array(report.findings.enumerated()), id: \.offset) { _, finding in
                HStack(alignment: .top, spacing: V.Space.s) {
                    Image(systemName: symbol(for: finding.status))
                        .foregroundColor(tone(for: finding.status))
                        .font(.system(size: 11, weight: .semibold))
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: V.Space.s) {
                            Text(finding.check).font(V.Font.label)
                                .foregroundColor(V.text)
                            if let code = finding.exit_code {
                                Text("exit \(code)")
                                    .font(V.Font.caption)
                                    .foregroundColor(code == 0 ? V.success : V.danger)
                            }
                        }
                        Text(finding.detail)
                            .font(V.Font.caption)
                            .foregroundColor(V.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                        if let command = finding.command, !command.isEmpty {
                            Text(command)
                                .font(V.Font.mono)
                                .foregroundColor(V.textTertiary)
                        }
                    }
                    Spacer()
                }
                .padding(V.Space.s)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(V.surface)
                .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
            }
        }
    }

    private func symbol(for status: String) -> String {
        switch status {
        case "ok": return "checkmark.circle.fill"
        case "problem": return "xmark.circle.fill"
        default: return "questionmark.circle"
        }
    }

    private func tone(for status: String) -> Color {
        switch status {
        case "ok": return V.success
        case "problem": return V.danger
        default: return V.warning
        }
    }

    // MARK: - Repair

    private func repairPanel(_ repair: APIModels.ProjectRepair) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Repair")
            Text(repair.summary)
                .font(V.Font.body)
                .foregroundColor(repair.applicable ? V.text : V.warning)
                .fixedSize(horizontal: false, vertical: true)
            if repair.applicable {
                Text("\(repair.operations.count) proposed change(s). Preview and approve "
                     + "them in ParallelWorld — nothing is applied here.")
                    .font(V.Font.caption)
                    .foregroundColor(V.textTertiary)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Transport

    private func inspect(runChecks: Bool = false) async {
        guard !path.isEmpty else { return }
        if runChecks { runningChecks = true } else { isLoading = true }
        errorMessage = nil
        repair = nil
        do {
            report = try await api.post(
                "/doctor/inspect",
                body: APIModels.DoctorInspectRequest(path: path, run_checks: runChecks),
                as: APIModels.ProjectReport.self)
        } catch {
            errorMessage = error.localizedDescription
            report = nil
        }
        isLoading = false
        runningChecks = false
    }

    private func proposeRepair() async {
        guard let report else { return }
        errorMessage = nil
        do {
            repair = try await api.post(
                "/doctor/repair",
                body: APIModels.DoctorRepairRequest(
                    path: report.root,
                    finding: report.problems.first?.detail ?? ""),
                as: APIModels.ProjectRepair.self)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}