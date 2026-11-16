import SwiftUI

/// Desktop Control: what Vantage can actually do to this Mac, and the controls
/// that do it.
///
/// The screen is built around one distinction it refuses to blur. Cursor
/// movement, clicking, typing and UI reading are **separately granted**
/// permissions, and a machine can have one without the others — this one does.
/// So each capability shows its own state, and a control whose capability is
/// missing is disabled with the reason attached rather than silently failing
/// when pressed.
///
/// The pointer controls call the real executor. A move is verified by reading
/// the pointer position back; a click cannot be verified from here at all,
/// because knowing whether a click *worked* means observing the target, so the
/// screen says "sent, unverified" and never paints a green tick for it.
struct DesktopControlView: View {
    @State private var posture: APIModels.DesktopPosture?
    @State private var moveResult: APIModels.DesktopResult?
    @State private var actionResult: APIModels.DesktopResult?
    @State private var loading = false
    @State private var working = false
    @State private var errorMessage: String?

    @State private var xText = "600"
    @State private var yText = "400"
    @State private var typeText = ""
    @State private var shortcutKey = "s"
    @State private var shortcutModifiers = "cmd"

    private let api = APIClient.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: V.Space.l) {
                PageHeader(
                    title: "Desktop Control",
                    subtitle: "Real pointer, keyboard and UI actions — only where macOS allows them."
                ) {
                    Button(loading ? "Reading…" : "Refresh") { Task { await load() } }
                        .buttonStyle(VSecondaryButtonStyle())
                        .disabled(loading)
                }

                if let errorMessage {
                    Text(errorMessage).font(V.Font.caption).foregroundColor(V.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let posture {
                    capabilityCard(posture)
                    pointerCard(posture)
                    keyboardCard(posture)
                    accessibilityCard(posture)
                } else if !loading {
                    Text("The desktop capability could not be read.").font(V.Font.body)
                        .foregroundColor(V.textSecondary)
                }

                if let moveResult { resultCard(moveResult, title: "Pointer") }
                if let actionResult { resultCard(actionResult, title: "Action") }
            }
            .padding(V.Space.xl)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(V.background)
        .task { await load() }
    }

    // MARK: - Capabilities

    @ViewBuilder
    private func capabilityCard(_ p: APIModels.DesktopPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack(alignment: .firstTextBaseline) {
                Text("WHAT macOS ALLOWS").font(V.Font.caption).foregroundColor(V.textTertiary)
                Spacer()
                Text(p.process_name ?? "").font(V.Font.mono).foregroundColor(V.textTertiary)
            }
            ForEach(p.capabilityRows, id: \.0) { row in
                HStack(spacing: V.Space.s) {
                    Image(systemName: row.1 ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .font(.system(size: 11))
                        .foregroundColor(row.1 ? V.success : V.warning)
                    Text(row.0).font(V.Font.label).foregroundColor(V.text)
                    Spacer()
                    if !row.1 {
                        Text("not granted").font(V.Font.caption).foregroundColor(V.warning)
                    }
                }
                .padding(.vertical, 1)
            }
            Text(p.note ?? "").font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            // The subject matters: a grant to this helper is not a grant to the
            // app in the Dock, and saying so is what stops the next confusing
            // debugging session.
            Text(p.subject_note ?? "").font(V.Font.caption).foregroundColor(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
            if let exe = p.executable, !exe.isEmpty {
                Text(exe).font(V.Font.mono).foregroundColor(V.textTertiary)
                    .lineLimit(1).truncationMode(.middle)
            }
            if !p.canClick {
                Button(working ? "Asking…" : "Request permission") {
                    Task { await requestAccess() }
                }
                .buttonStyle(VSecondaryButtonStyle())
                .disabled(working)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Pointer

    @ViewBuilder
    private func pointerCard(_ p: APIModels.DesktopPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("POINTER").font(V.Font.caption).foregroundColor(V.textTertiary)
            HStack(spacing: V.Space.s) {
                field("x", text: $xText)
                field("y", text: $yText)
                Button("Move cursor") { Task { await move() } }
                    .buttonStyle(VPrimaryButtonStyle())
                    .disabled(working || !p.canMoveCursor)
                Button("Click") { Task { await click() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(working || !p.canClick)
                Button("Double") { Task { await doubleClick() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(working || !p.canClick)
                Button("Right") { Task { await rightClick() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(working || !p.canClick)
            }
            if !p.canClick {
                Text("Clicking is disabled because this executable has no permission "
                     + "to post input events. Moving the cursor does not need it.")
                    .font(V.Font.caption).foregroundColor(V.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Keyboard

    @ViewBuilder
    private func keyboardCard(_ p: APIModels.DesktopPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("KEYBOARD").font(V.Font.caption).foregroundColor(V.textTertiary)
            HStack(spacing: V.Space.s) {
                TextField("Text to type", text: $typeText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 240)
                Button("Type") { Task { await typeTextAction() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(working || !p.canType || typeText.isEmpty)
            }
            HStack(spacing: V.Space.s) {
                TextField("modifiers", text: $shortcutModifiers)
                    .textFieldStyle(.roundedBorder).frame(width: 100)
                TextField("key", text: $shortcutKey)
                    .textFieldStyle(.roundedBorder).frame(width: 60)
                Button("Shortcut") { Task { await shortcut() } }
                    .buttonStyle(VSecondaryButtonStyle())
                    .disabled(working || !p.canType)
            }
            Text("Typing goes to whatever field macOS has focused. Vantage never "
                 + "types into password fields, Touch ID prompts or security dialogs.")
                .font(V.Font.caption).foregroundColor(V.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
            if !p.canType {
                Text("Typing is disabled: this executable cannot post input events.")
                    .font(V.Font.caption).foregroundColor(V.warning)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Accessibility

    @ViewBuilder
    private func accessibilityCard(_ p: APIModels.DesktopPosture) -> some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            Text("INTERFACE").font(V.Font.caption).foregroundColor(V.textTertiary)
            Text(p.canReadUI
                 ? "Vantage can read the Accessibility tree, so it can find a button by "
                   + "its label and press it through the application's own action."
                 : "Reading the interface needs Accessibility access, which this "
                   + "executable does not have. Without it Vantage cannot find "
                   + "elements by name, only by coordinate.")
                .font(V.Font.caption)
                .foregroundColor(p.canReadUI ? V.textSecondary : V.warning)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surface)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    // MARK: - Result

    @ViewBuilder
    private func resultCard(_ r: APIModels.DesktopResult, title: String) -> some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            HStack(spacing: V.Space.s) {
                Image(systemName: symbol(r.status))
                    .font(.system(size: 12)).foregroundColor(color(r.status))
                Text("\(title): \(r.status)").font(V.Font.label)
                    .foregroundColor(color(r.status))
                Spacer()
            }
            Text(r.reason).font(V.Font.caption).foregroundColor(V.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if let before = r.cursor_before, let after = r.cursor_after {
                Text("pointer (\(Int(before.x)), \(Int(before.y))) → (\(Int(after.x)), \(Int(after.y)))")
                    .font(V.Font.mono).foregroundColor(V.textTertiary)
            }
        }
        .padding(V.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(V.surfaceAlt)
        .clipShape(RoundedRectangle(cornerRadius: V.Radius.card))
    }

    private func symbol(_ status: String) -> String {
        switch status {
        case "verified": return "checkmark.circle.fill"
        case "performed_unverified": return "questionmark.circle.fill"
        case "permission_required", "blocked": return "lock.fill"
        default: return "xmark.circle.fill"
        }
    }

    private func color(_ status: String) -> Color {
        switch status {
        case "verified": return V.success
        // Sent but not observed is amber, never green: the whole point.
        case "performed_unverified": return V.warning
        case "permission_required", "blocked": return V.warning
        default: return V.danger
        }
    }

    @ViewBuilder
    private func field(_ label: String, text: Binding<String>) -> some View {
        HStack(spacing: 4) {
            Text(label).font(V.Font.caption).foregroundColor(V.textTertiary)
            TextField(label, text: text)
                .textFieldStyle(.roundedBorder).frame(width: 70)
        }
    }

    // MARK: - Data

    private func load() async {
        loading = true
        errorMessage = nil
        do {
            posture = try await api.get("/desktop", as: APIModels.DesktopPosture.self)
        } catch {
            errorMessage = "The desktop capability could not be read: \(error.localizedDescription)"
        }
        loading = false
    }

    private func requestAccess() async {
        working = true
        do {
            let r = try await api.post("/desktop/request-access", body: EmptyBody(),
                                       as: APIModels.DesktopResult.self)
            actionResult = r
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }

    private func move() async {
        working = true
        do {
            moveResult = try await api.post("/desktop/move",
                                            body: PointBody(x: Double(xText) ?? 0,
                                                            y: Double(yText) ?? 0, smooth: false,
                                                            durationMs: 0),
                                            as: APIModels.DesktopResult.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }

    private func click() async {
        working = true
        do {
            actionResult = try await api.post("/desktop/click",
                                              body: ClickBody(x: Double(xText) ?? 0,
                                                              y: Double(yText) ?? 0,
                                                              button: "left", count: 1),
                                              as: APIModels.DesktopResult.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }

    private func doubleClick() async {
        working = true
        do {
            actionResult = try await api.post("/desktop/click",
                                              body: ClickBody(x: Double(xText) ?? 0,
                                                              y: Double(yText) ?? 0,
                                                              button: "left", count: 2),
                                              as: APIModels.DesktopResult.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }

    private func rightClick() async {
        working = true
        do {
            actionResult = try await api.post("/desktop/click",
                                              body: ClickBody(x: Double(xText) ?? 0,
                                                              y: Double(yText) ?? 0,
                                                              button: "right", count: 1),
                                              as: APIModels.DesktopResult.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }

    private func typeTextAction() async {
        working = true
        do {
            actionResult = try await api.post("/desktop/type", body: TextBody(text: typeText),
                                              as: APIModels.DesktopResult.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }

    private func shortcut() async {
        working = true
        do {
            let mods = shortcutModifiers.split(separator: ",").map {
                $0.trimmingCharacters(in: .whitespaces)
            }.filter { !$0.isEmpty }
            actionResult = try await api.post("/desktop/key",
                                              body: KeyBody(key: shortcutKey, modifiers: mods),
                                              as: APIModels.DesktopResult.self)
        } catch {
            errorMessage = error.localizedDescription
        }
        working = false
    }
}

private struct EmptyBody: Codable {}
private struct PointBody: Codable { let x: Double; let y: Double; let smooth: Bool; let durationMs: Int }
private struct ClickBody: Codable { let x: Double; let y: Double; let button: String; let count: Int }
private struct TextBody: Codable { let text: String }
private struct KeyBody: Codable { let key: String; let modifiers: [String] }