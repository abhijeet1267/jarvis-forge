import SwiftUI

@available(iOS 16.0, *)
struct DevicesView: View {
    @StateObject private var client = CompanionAPIClient.shared
    @State private var devices: [DeviceInfo] = []
    @State private var isLoading = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Group {
                if isLoading && devices.isEmpty { ProgressView("Loading devices…") }
                else if devices.isEmpty { emptyState }
                else { deviceList }
            }
            .navigationTitle("Devices")
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 16) {
            Image(systemName: "iphone.gen3.slash").font(.system(size: 48)).foregroundStyle(.secondary)
            Text("No Paired Devices").font(.title3).fontWeight(.semibold)
            Text("Pair a device from this phone or the Mac dashboard.").font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center).padding(.horizontal, 32)
        }
    }

    private var deviceList: some View {
        List {
            if let error = error { Text(error).foregroundStyle(.red).font(.callout) }
            ForEach(devices) { device in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Image(systemName: device.isPhone ? "iphone" : "laptopcomputer")
                            .foregroundStyle(device.isPhone ? .blue : .teal)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(device.displayName).font(.headline)
                            Text(device.id).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer()
                        statusBadge(device.state)
                    }
                    if let paired = device.paired_at {
                        Label("Paired: \(relativeDate(paired))", systemImage: "clock").font(.caption).foregroundStyle(.secondary)
                    }
                    if let seen = device.last_seen {
                        Label("Last seen: \(relativeDate(seen))", systemImage: "eye").font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 4)
            }
        }
        .listStyle(.plain)
    }

    private func statusBadge(_ state: String?) -> some View {
        let color: Color = state == "paired" ? .green : state == "pending" ? .orange : state == "revoked" ? .red : .secondary
        return Text(state?.capitalized ?? "Unknown")
            .font(.caption2).fontWeight(.medium)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(color.opacity(0.15)).foregroundColor(color).cornerRadius(6)
    }

    private func load() async {
        isLoading = true; error = nil
        do { devices = try await client.fetchDevices() }
        catch { error = (error as? CompanionClientError)?.errorDescription ?? error.localizedDescription }
        isLoading = false
    }

    private func relativeDate(_ iso: String) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: iso) { return d.formatted(.relative(presentation: .named)) }
        f.formatOptions = [.withInternetDateTime]
        if let d = f.date(from: iso) { return d.formatted(.relative(presentation: .named)) }
        return iso
    }
}