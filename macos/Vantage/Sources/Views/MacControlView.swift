import SwiftUI

/// Mac Control: what is running, what Vantage can launch, and what it observed.
///
/// The screen is built around one distinction the backend insists on: a launch
/// command being accepted is not the same as an application running. When the
/// two disagree, this shows the difference rather than a green tick -- "launched
/// but not observed running" is a real state and a person needs to see it.
///
/// Only applications in Vantage's registry can be launched. That is a closed
/// list, so a name this screen does not offer cannot become a blind launch.
struct MacControlView: View {

    @State private var status: APIModels.MacStatus?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var launching: String?
    @State private var result: APIModels.MacLaunchResult?
    @State private var filter = ""

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Mac Control",
                           subtitle: "Applications Vantage can start, and what it observed.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await load() } })
                }

                if let status {
                    availability(status)
                    if let result { resultPanel(result) }
                    applications(status)
                }
            }
            .padding(V.Space.gutter)
        }
        .background(V.background)
        .task { await load() }
    }

    /// What this machine can and cannot do, said plainly.
    private func availability(_ status: APIModels.MacStatus) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Availability")
            HStack(spacing: V.Space.l) {
                VStack(alignment: .leading, spacing: 0) {
                    Text("\(status.supported.count)").font(V.Font.cardTitle)
                        .foregroundColor(V.text)
                    Text("launchable").font(V.Font.caption)
                        .foregroundColor(V.textTertiary)
                }
                VStack(alignment: .leading, spacing: 0) {
                    Text("\(status.running.count)").font(V.Font.cardTitle)
                        .foregroundColor(status.can_list_running ? V.success : V.warning)
                    Text("running").font(V.Font.caption)
                        .foregroundColor(V.textTertiary)
                }
                Spacer()
            }
            Text(status.note)
                .font(V.Font.caption)
                .foregroundColor(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func applications(_ status: APIModels.MacStatus) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            SectionHeader("Applications")
            TextField("Filter", text: $filter)
                .textFieldStyle(.roundedBorder)
                .font(V.Font.body)

            ForEach(filtered(status)) { app in
                HStack(spacing: V.Space.s) {
                    Image(systemName: app.running ? "circle.fill" : "circle")
                        .font(.system(size: 8))
                        .foregroundColor(app.running ? V.success : V.textTertiary)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(app.name).font(V.Font.label).foregroundColor(V.text)
                        Text(app.running ? "running" : "not running")
                            .font(V.Font.caption).foregroundColor(V.textTertiary)
                    }
                    Spacer()
                    Button(launching == app.key ? "Starting…" : "Open") {
                        Task { await open(app) }
                    }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(launching != nil || app.running)
                }
                .padding(V.Space.s)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(V.surface)
                .clipShape(RoundedRectangle(cornerRadius: V.Radius.control))
            }
        }
    }

    private func filtered(_ status: APIModels.MacStatus) -> [APIModels.MacApp] {
        let query = filter.trimmingCharacters(in: .whitespaces).lowercased()
        guard !query.isEmpty else { return status.supported }
        return status.supported.filter { $0.name.lowercased().contains(query) }
    }

    /// The result states "launched" and "observed running" separately, because
    /// they are different facts and the difference is the whole point.
    private func resultPanel(_ result: APIModels.MacLaunchResult) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xxs) {
            SectionHeader("Last action")
            HStack(spacing: V.Space.s) {
                Image(systemName: result.observed_running
                      ? "checkmark.circle.fill"
                      : "exclamationmark.triangle.fill")
                    .foregroundColor(result.observed_running ? V.success : V.warning)
                Text(result.detail)
                    .font(V.Font.body)
                    .foregroundColor(result.observed_running ? V.text : V.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            status = try await api.get("/mac/control", as: APIModels.MacStatus.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func open(_ app: APIModels.MacApp) async {
        launching = app.key
        errorMessage = nil
        defer { launching = nil }
        do {
            result = try await api.post(
                "/mac/open", body: APIModels.MacOpenRequest(app: app.key),
                as: APIModels.MacLaunchResult.self)
            await load()
        } catch {
            // A refusal carries the backend's own reason (409), which is more
            // useful than a generic failure message.
            errorMessage = error.localizedDescription
        }
    }
}