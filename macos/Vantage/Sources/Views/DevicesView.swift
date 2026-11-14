import SwiftUI

/// Device listing, pairing requests, and revocation.
struct DevicesView: View {

    @State private var devices: [APIModels.Device] = []
    @State private var pendingPairings: [APIModels.PairingRequest] = []
    @State private var isLoading = true
    @State private var errorMessage: String?

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(title: "Devices",
                           subtitle: "Paired phones and computers, and what they may do.")

                if let error = errorMessage {
                    ErrorBanner(message: error, retry: { Task { await load() } })
                }

                // Paired devices
                SectionHeader("Paired Devices")
                if devices.isEmpty && !isLoading {
                    // "No devices paired" is a claim about the backend, so it
                    // may only be made when the read actually succeeded. A
                    // failed read used to render it directly under the error
                    // banner, which would tell the owner their phone was
                    // unpaired when the truth was that nothing was checked.
                    if errorMessage != nil {
                        Text("I could not read the device list, so I cannot say whether anything is paired.")
                            .font(.callout)
                            .foregroundStyle(V.textSecondary)
                            .padding(.vertical, 8)
                    } else {
                        Text("No devices paired.")
                            .font(.callout)
                            .foregroundStyle(V.textSecondary)
                            .padding(.vertical, 8)
                    }
                }
                ForEach(devices) { device in
                    DeviceCard(device: device)
                }

                // Pending pairings
                if !pendingPairings.isEmpty {
                    SectionHeader("Pending Pairings")
                    ForEach(pendingPairings) { pairing in
                        PairingRequestCard(pairing: pairing) { approved in
                            await handlePairing(pairing.pairing_id, approved: approved)
                        }
                    }
                }
            }
            .padding(.horizontal, V.Space.gutter)
            .padding(.vertical, V.Space.section)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .background(V.background)
        .navigationTitle("Devices")
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            async let devs: [APIModels.Device] = api.get("/devices")
            async let pairings: [APIModels.PairingRequest] = api.get("/devices/pair/pending")
            (devices, pendingPairings) = try await (devs, pairings)
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func handlePairing(_ pairingId: String, approved: Bool) async {
        let action = approved ? "approve" : "deny"
        do {
            let _: APIModels.ConfirmationResponse = try await api.post("/devices/pair/\(pairingId)/\(action)")
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - Device card

struct DeviceCard: View {
    let device: APIModels.Device
    @State private var showRevokeConfirm = false
    private let api = APIClient.shared

    var body: some View {
        Card {
            HStack(spacing: V.Space.m) {
                Image(systemName: deviceIcon)
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(V.accent)
                    .frame(width: 26)

                VStack(alignment: .leading, spacing: 2) {
                    Text(device.displayName)
                        .font(V.Font.cardTitle)
                        .foregroundStyle(V.text)
                    Text(device.deviceType)
                        .font(V.Font.caption)
                        .foregroundStyle(V.textSecondary)
                }

                Spacer()

                if device.is_host != true {
                    Button("Revoke") {
                        showRevokeConfirm = true
                    }
                    .buttonStyle(VDestructiveButtonStyle())
                } else {
                    StatusPill(kind: .connected, text: "Host")
                }
            }
        }
        .vConfirm("Revoke Device?",
                  message: "Revoke access for \(device.device_id)? This cannot be undone.",
                  confirm: "Revoke",
                  isPresented: $showRevokeConfirm) {
            Task { await revokeDevice() }
        }
    }

    /// The icon for the device's kind.
    ///
    /// The names are the **server's**: it reports `computer`, `android`,
    /// `smartwatch`, `vehicle` and `home_hub`. The previous switch knew
    /// `phone` / `watch` / `car` / `home` -- names the server has never
    /// sent -- so every device fell through to the laptop icon.
    private var deviceIcon: String {
        switch device.deviceType.lowercased() {
        case "phone", "android", "ios": return "iphone"
        case "watch", "smartwatch": return "applewatch"
        case "car", "vehicle": return "car.fill"
        case "home", "home_hub": return "house.fill"
        case "tablet": return "ipad"
        case "computer", "macos", "desktop": return "desktopcomputer"
        default: return "laptopcomputer"
        }
    }

    private func revokeDevice() async {
        do {
            let _: [String: String] = try await api.post("/devices/\(device.device_id)/revoke")
        } catch { }
    }
}

// MARK: - Pairing request card

struct PairingRequestCard: View {
    let pairing: APIModels.PairingRequest
    var onAction: (Bool) async -> Void

    @State private var isLoading = false

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "iphone.gen3")
                .font(.title3)
                .foregroundStyle(V.warning)

            VStack(alignment: .leading, spacing: 2) {
                Text(pairing.device_name ?? "Unknown device")
                    .font(.callout)
                Text("Wants to pair")
                    .font(.caption)
                    .foregroundStyle(V.textSecondary)
            }

            Spacer()

            HStack(spacing: 8) {
                Button("Deny") {
                    Task { await onAction(false) }
                }
                .buttonStyle(.plain)
                .font(.caption)
                .foregroundStyle(V.textSecondary)
                Button("Approve") {
                    Task { await onAction(true) }
                }
                .buttonStyle(.plain)
                .font(.caption)
                .foregroundStyle(V.accent)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: V.Radius.control).fill(V.warningSoft))
    }
}