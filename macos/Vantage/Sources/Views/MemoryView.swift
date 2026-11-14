import SwiftUI

/// Memory inspection, project browsing, and manual memory entry.
struct MemoryView: View {

    @State private var selectedTab: MemoryTab = .items

    var body: some View {
        VStack(spacing: 0) {
            PageHeader(title: "Memory", subtitle: "What Vantage remembers, and the projects it knows.")
                .padding(.horizontal, V.Space.gutter)
                .padding(.top, V.Space.section)
                .padding(.bottom, V.Space.l)
                .frame(maxWidth: 760)
                .frame(maxWidth: .infinity, alignment: .center)

            Picker("View", selection: $selectedTab) {
                Text("Memory").tag(MemoryTab.items)
                Text("Projects").tag(MemoryTab.projects)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, V.Space.gutter)
            .padding(.bottom, V.Space.s)
            .frame(maxWidth: 760, alignment: .leading)
            .frame(maxWidth: 300)

            switch selectedTab {
            case .items: MemoryItemsList()
            case .projects: ProjectsList()
            }
        }
        .navigationTitle("Memory")
    }
}

enum MemoryTab { case items, projects }

// MARK: - Memory items

struct MemoryItemsList: View {
    @State private var response: APIModels.MemoryResponse?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var newContent = ""
    @State private var showAddField = false

    private let api = APIClient.shared

    var body: some View {
        VStack(spacing: 0) {
            if let error = errorMessage {
                ErrorBanner(message: error, retry: { Task { await load() } })
                    .padding(.horizontal, 24)
                    .padding(.top, 8)
            }

            // Stats bar
            //
            // The backend returns a scope -> count map with a "total" key.
            // Rendered raw, that was a single unstyled pill reading "total: 0"
            // at the top of an otherwise empty screen -- a raw key:value dump
            // rather than a summary. Give the total the prominence it earns and
            // the per-scope counts the rest.
            //
            // Only when there is something to summarise: with no items the
            // empty state below already says so, and a "Nothing remembered
            // yet" line above it said the same thing twice.
            if let stats = response?.stats, !stats.isEmpty,
               (response?.items?.isEmpty == false) {
                HStack(spacing: 8) {
                    if let rawTotal = stats["total"]?.value {
                        let total = String(describing: rawTotal)
                        Text(total == "0" ? "Nothing remembered yet" : "\(total) remembered")
                            .font(.caption)
                            .foregroundStyle(total == "0" ? V.textSecondary : V.text)
                    }
                    // The per-scope counts, except the one that just restates
                    // the total: with every item long-term, the bar read
                    // "3 remembered  Long term 3".
                    let scopes = stats.keys.sorted().filter { $0 != "total" }
                    let nonZero = scopes.filter { String(describing: stats[$0]?.value ?? "") != "0" }
                    ForEach(Array(scopes.count == 1 ? [] : nonZero), id: \.self) { key in
                        if let val = stats[key]?.value {
                            Text("\(MemoryRow.humanised(key)) \(String(describing: val))")
                                .font(.caption2)
                                .foregroundStyle(V.textSecondary)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 3)
                                .background(Capsule().fill(V.fill))
                        }
                    }
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            // Add memory bar
            HStack {
                if showAddField {
                    TextField("New memory…", text: $newContent)
                        .textFieldStyle(.roundedBorder)
                    Button("Save") {
                        Task {
                            await addMemory()
                            showAddField = false
                        }
                    }
                    .disabled(newContent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button("Cancel") { showAddField = false }
                        .buttonStyle(.plain)
                        .foregroundStyle(V.textSecondary)
                } else {
                    Button(action: { showAddField = true }) {
                        Label("Add Memory", systemImage: "plus")
                            .font(.caption)
                    }
                    Spacer()
                }
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 8)

            if isLoading {
                Spacer()
                ProgressView()
                Spacer()
            } else if let items = response?.items, !items.isEmpty {
                List(items) { item in
                    MemoryRow(item: item)
                }
                .listStyle(.inset)
            } else {
                Spacer()
                // A failed read left `response` nil, which this branch used to
                // render as "No memory items" -- a claim about stored memory
                // that nothing had actually checked.
                if errorMessage != nil {
                    Text("I could not read memory, so this is not an empty list.")
                        .foregroundStyle(V.textSecondary)
                } else {
                    Text("No memory items.")
                        .foregroundStyle(V.textSecondary)
                }
                Spacer()
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            response = try await api.get("/memory", as: APIModels.MemoryResponse.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func addMemory() async {
        let content = newContent.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !content.isEmpty else { return }
        do {
            let body = APIModels.CreateMemoryRequest(content: content)
            let _: APIModels.MemoryItem = try await api.post("/memory", body: body)
            newContent = ""
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

struct MemoryRow: View {
    let item: APIModels.MemoryItem

    /// "long_term" -> "Long term", so the badge reads as a label.
    static func humanised(_ key: String) -> String {
        let words = key.replacingOccurrences(of: "_", with: " ")
        return words.prefix(1).uppercased() + words.dropFirst()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(item.content ?? "(empty)")
                .font(.callout)
            HStack(spacing: 8) {
                if let scope = item.scope {
                    // The scope is a database token ("long_term"); a badge is
                    // read by a person, so it is shown as words.
                    Text(MemoryRow.humanised(scope))
                        .font(.caption2)
                        .foregroundStyle(V.textSecondary)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(V.accent.opacity(0.1)))
                }
                if let created = item.created_at {
                    Text(created)
                        .font(.caption2)
                        .foregroundStyle(V.textSecondary)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Projects

struct ProjectsList: View {
    @State private var projects: [APIModels.ProjectInfo] = []
    @State private var isLoading = true
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        VStack {
            if let error = errorMessage {
                ErrorBanner(message: error, retry: { Task { await load() } })
                    .padding(.horizontal, 24)
            }

            if isLoading {
                Spacer()
                ProgressView()
                Spacer()
            } else if projects.isEmpty {
                Spacer()
                if errorMessage != nil {
                    Text("I could not read the project list, so this is not an empty list.")
                        .foregroundStyle(V.textSecondary)
                } else {
                    Text("No projects.")
                        .foregroundStyle(V.textSecondary)
                }
                Spacer()
            } else {
                List(projects) { project in
                    HStack {
                        Image(systemName: "folder.fill")
                            .foregroundStyle(V.accent)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(project.name ?? "Unnamed")
                                .font(.callout)
                            if let desc = project.description {
                                Text(desc)
                                    .font(.caption)
                                    .foregroundStyle(V.textSecondary)
                                    .lineLimit(1)
                            }
                        }
                    }
                    .padding(.vertical, 2)
                }
                .listStyle(.inset)
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            projects = try await api.get("/projects", as: [APIModels.ProjectInfo].self)
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}