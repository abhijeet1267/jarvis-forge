import SwiftUI

/// Shared controls. Each one exists because at least two real screens need the
/// same thing and would otherwise drift: a status dot that means four different
/// greens depending on who drew it, a card with its own radius and shadow, a
/// primary button that is not the same green twice.
///
/// These wrap native controls where native is right (Toggle, TextField, Picker)
/// and only draw what SwiftUI does not provide a premium default for.

// MARK: - Page chrome

/// The heading every screen shares: title, optional subtitle, trailing actions.
struct PageHeader<Actions: View>: View {
    let title: String
    var subtitle: String? = nil
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.l) {
            VStack(alignment: .leading, spacing: V.Space.xs) {
                Text(title).font(V.Font.pageTitle).foregroundStyle(V.text)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(V.Font.body)
                        .foregroundStyle(V.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: V.Space.l)
            actions()
        }
    }
}

extension PageHeader where Actions == EmptyView {
    init(title: String, subtitle: String? = nil) {
        self.init(title: title, subtitle: subtitle, actions: { EmptyView() })
    }
}

/// A titled region with consistent spacing above the content.
struct SectionBlock<Content: View>: View {
    let title: String
    var trailing: AnyView? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.s) {
            HStack {
                Text(title.uppercased())
                    .font(V.Font.caption.weight(.semibold))
                    .tracking(0.6)
                    .foregroundStyle(V.textTertiary)
                Spacer()
                if let trailing { trailing }
            }
            content()
        }
    }
}

// MARK: - Surfaces

/// The standard card. A border rather than a heavy shadow, so a screen with
/// eight cards does not look like a stack of floating panels.
struct Card<Content: View>: View {
    var padding: CGFloat = V.Space.l
    var tone: CardTone = .plain
    @ViewBuilder var content: () -> Content

    enum CardTone { case plain, ivory, gold, accent, danger, warning }

    var body: some View {
        content()
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(background, in: RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: V.Radius.card, style: .continuous)
                    .strokeBorder(outline, lineWidth: 1)
            )
    }

    private var background: Color {
        switch tone {
        case .plain: return V.surface
        case .ivory: return V.surfaceAlt
        case .gold: return V.goldSoft
        case .accent: return V.accentSoft
        case .danger: return V.dangerSoft
        case .warning: return V.warningSoft
        }
    }

    private var outline: Color {
        switch tone {
        case .gold: return V.goldBorder
        case .accent: return V.accent.opacity(0.25)
        case .danger: return V.danger.opacity(0.25)
        case .warning: return V.warning.opacity(0.25)
        case .plain, .ivory: return V.border
        }
    }
}

/// A key/value row used by Settings and Connections. Value wraps; the label
/// keeps its width so a column of rows lines up.
struct DetailRow: View {
    let label: String
    let value: String
    var tone: Color? = nil
    var monospaced = false

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.m) {
            Text(label)
                .font(V.Font.label)
                .foregroundStyle(V.textSecondary)
                .frame(width: 104, alignment: .leading)
            Text(value)
                .font(monospaced ? V.Font.mono : V.Font.body)
                .foregroundStyle(tone ?? V.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }
}

// MARK: - Status

enum StatusKind {
    case connected, pending, setup, error, idle

    var colour: Color {
        switch self {
        case .connected: return V.accent
        case .pending: return V.gold
        case .setup: return V.textTertiary
        case .error: return V.danger
        case .idle: return V.textTertiary
        }
    }

    var symbol: String {
        switch self {
        case .connected: return "checkmark.circle.fill"
        case .pending: return "clock.fill"
        case .setup: return "ellipsis.circle"
        case .error: return "exclamationmark.triangle.fill"
        case .idle: return "circle.dashed"
        }
    }
}

/// The one status pill. The dot and the word always agree, because they are
/// driven by the same `StatusKind`; a screen cannot show a green dot beside
/// "Not connected" without changing the kind it passes.
struct StatusPill: View {
    let kind: StatusKind
    let text: String

    var body: some View {
        HStack(spacing: V.Space.xs) {
            Image(systemName: kind.symbol)
                .font(.system(size: 10, weight: .semibold))
            Text(text).font(V.Font.caption.weight(.medium))
        }
        .foregroundStyle(kind.colour)
        .padding(.horizontal, V.Space.s)
        .padding(.vertical, 3)
        .background(kind.colour.opacity(0.10), in: Capsule())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(text)")
    }
}

/// Inline feedback: success, warning, error, or plain information. Replaces the
/// hand-rolled coloured rectangles that had different radii on every screen.
struct Notice: View {
    enum Kind { case info, success, warning, error
        var colour: Color {
            switch self {
            case .info: return V.textSecondary
            case .success: return V.accent
            case .warning: return V.warning
            case .error: return V.danger
            }
        }
        var symbol: String {
            switch self {
            case .info: return "info.circle.fill"
            case .success: return "checkmark.circle.fill"
            case .warning: return "exclamationmark.triangle.fill"
            case .error: return "exclamationmark.octagon.fill"
            }
        }
    }

    let kind: Kind
    let message: String
    var actionTitle: String? = nil
    var action: (() -> Void)? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: V.Space.s) {
            Image(systemName: kind.symbol)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(kind.colour)
            Text(message)
                .font(V.Font.body)
                .foregroundStyle(V.text)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: V.Space.s)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(VQuietButtonStyle())
            }
        }
        .padding(.horizontal, V.Space.m)
        .padding(.vertical, V.Space.s + 2)
        .background(kind.colour.opacity(0.08), in: RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                .strokeBorder(kind.colour.opacity(0.22), lineWidth: 1)
        )
    }
}

// MARK: - Empty / loading

struct EmptyState: View {
    let symbol: String
    let title: String
    let message: String
    var actionTitle: String? = nil
    var action: (() -> Void)? = nil

    var body: some View {
        VStack(spacing: V.Space.s) {
            Image(systemName: symbol)
                .font(.system(size: 26, weight: .light))
                .foregroundStyle(V.gold)
            Text(title).font(V.Font.cardTitle).foregroundStyle(V.text)
            Text(message)
                .font(V.Font.body)
                .foregroundStyle(V.textSecondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 420)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(VPrimaryButtonStyle())
                    .padding(.top, V.Space.xs)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(V.Space.xxl)
    }
}

/// A quiet inline spinner with a sentence, used while a real request is in
/// flight. Never used as a placeholder for a state that is actually known.
struct LoadingRow: View {
    let text: String
    var body: some View {
        HStack(spacing: V.Space.s) {
            ProgressView().controlSize(.small)
            Text(text).font(V.Font.body).foregroundStyle(V.textSecondary)
        }
    }
}

// MARK: - Controls

/// Small labelled text field with the shared surface and focus treatment.
struct VField: View {
    let label: String?
    let placeholder: String
    @Binding var text: String
    var onSubmit: (() -> Void)? = nil
    var monospaced = false

    @FocusState private var focused: Bool

    init(label: String? = nil, placeholder: String, text: Binding<String>,
         monospaced: Bool = false, onSubmit: (() -> Void)? = nil) {
        self.label = label
        self.placeholder = placeholder
        self._text = text
        self.monospaced = monospaced
        self.onSubmit = onSubmit
    }

    var body: some View {
        VStack(alignment: .leading, spacing: V.Space.xs) {
            if let label {
                Text(label).font(V.Font.label).foregroundStyle(V.textSecondary)
            }
            TextField(placeholder, text: $text)
                .textFieldStyle(.plain)
                .font(monospaced ? V.Font.mono : V.Font.body)
                .focused($focused)
                .onSubmit { onSubmit?() }
                .padding(.horizontal, V.Space.m)
                .padding(.vertical, 7)
                .background(V.surface, in: RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                        .strokeBorder(focused ? V.accent : V.border,
                                      lineWidth: focused ? 1.5 : 1)
                )
                .animation(V.motionAllowed ? V.quick : nil, value: focused)
        }
    }
}

/// A labelled switch row. The label and the explanation sit together so a
/// setting is never a bare toggle whose meaning lives in another screen.
struct VToggleRow: View {
    let title: String
    let detail: String
    @Binding var isOn: Bool
    var disabled = false
    var disabledReason: String? = nil

    var body: some View {
        HStack(alignment: .top, spacing: V.Space.l) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(V.Font.cardTitle).foregroundStyle(V.text)
                Text(disabled ? (disabledReason ?? detail) : detail)
                    .font(V.Font.body)
                    .foregroundStyle(V.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: V.Space.m)
            Toggle("", isOn: $isOn)
                .labelsHidden()
                .toggleStyle(.switch)
                .tint(V.accent)
                .disabled(disabled)
                .accessibilityLabel(title)
        }
    }
}

// MARK: - Button styles

struct VPrimaryButtonStyle: ButtonStyle {
    var fullWidth = false
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(V.Font.label)
            // Disabled reads as disabled, not as a dimmed call to action: an
            // emerald fill at 40% still reads "press me" while the button does
            // nothing. A neutral fill with secondary text states the same thing
            // without inviting the press.
            .foregroundStyle(enabled ? Color.white : V.textSecondary)
            .padding(.horizontal, V.Space.l)
            .padding(.vertical, 7)
            .frame(maxWidth: fullWidth ? .infinity : nil)
            .background(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .fill(enabled
                          ? (configuration.isPressed ? V.accentPressed : V.accent)
                          : V.surfaceAlt)
                    .overlay(
                        RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                            .strokeBorder(enabled ? Color.clear : V.border, lineWidth: 1))
            )
            .contentShape(RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
            .scaleEffect(configuration.isPressed && V.motionAllowed ? 0.985 : 1)
            .animation(V.motionAllowed ? V.quick : nil, value: configuration.isPressed)
    }
}

/// Secondary: a bordered control on the surface, used beside a primary action.
struct VSecondaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(V.Font.label)
            .foregroundStyle(V.text)
            .padding(.horizontal, V.Space.l)
            .padding(.vertical, 7)
            .background(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .fill(configuration.isPressed ? V.fill : V.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .strokeBorder(V.border, lineWidth: 1)
            )
            .opacity(enabled ? 1 : 0.45)
            .contentShape(RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
    }
}

/// Tertiary: text-only, for row actions and "Retry"-style affordances.
struct VQuietButtonStyle: ButtonStyle {
    var tint: Color = V.accent
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(V.Font.label)
            .foregroundStyle(tint.opacity(configuration.isPressed ? 0.7 : 1))
            .padding(.horizontal, V.Space.s)
            .padding(.vertical, V.Space.xs)
            .background(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .fill(configuration.isPressed ? tint.opacity(0.08) : .clear)
            )
            .opacity(enabled ? 1 : 0.4)
            .contentShape(RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
    }
}

/// Destructive: used for disconnect, delete, revoke. Red text, quiet surface, so
/// it reads as a deliberate act without dominating the screen.
struct VDestructiveButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(V.Font.label)
            .foregroundStyle(V.danger)
            .padding(.horizontal, V.Space.m)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .fill(configuration.isPressed ? V.dangerSoft : .clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                    .strokeBorder(V.danger.opacity(0.28), lineWidth: 1)
            )
            .opacity(enabled ? 1 : 0.4)
            .contentShape(RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
    }
}

/// An icon-only button that always carries a tooltip and an accessibility label —
/// the two things the earlier icon buttons were missing.
struct IconAction: View {
    let symbol: String
    let help: String
    var tint: Color = V.textSecondary
    var isActive = false
    let action: () -> Void

    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(isActive ? V.accent : tint)
                .frame(width: 28, height: 26)
                .background(
                    RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous)
                        .fill(hovering ? V.fill : .clear)
                )
                .contentShape(RoundedRectangle(cornerRadius: V.Radius.control, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(help)
        .accessibilityLabel(help)
        .animation(V.motionAllowed ? V.quick : nil, value: hovering)
    }
}

/// A compact labelled switch for a toolbar, e.g. "AI voice: On".
struct InlineToggleChip: View {
    let title: String
    let valueText: String
    @Binding var isOn: Bool
    var symbol: String
    var disabled = false

    var body: some View {
        Button {
            guard !disabled else { return }
            V.animate { isOn.toggle() }
        } label: {
            HStack(spacing: V.Space.xs + 1) {
                Image(systemName: symbol)
                    .font(.system(size: 11, weight: .semibold))
                Text("\(title): \(valueText)")
                    .font(V.Font.caption.weight(.medium))
            }
            .foregroundStyle(disabled ? V.textTertiary : (isOn ? V.accent : V.textSecondary))
            .padding(.horizontal, V.Space.s + 1)
            .padding(.vertical, 4)
            .background(
                Capsule().fill(isOn && !disabled ? V.accentSoft : V.fill)
            )
            .overlay(
                Capsule().strokeBorder(isOn && !disabled ? V.accent.opacity(0.30) : V.border,
                                       lineWidth: 1)
            )
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .help("\(title) — currently \(valueText)")
        .accessibilityLabel(title)
        .accessibilityValue(valueText)
    }
}

/// Prompt asking before a destructive or irreversible act.
struct ConfirmDialog: ViewModifier {
    let title: String
    let message: String
    let confirmTitle: String
    @Binding var isPresented: Bool
    var destructive = true
    let onConfirm: () -> Void

    func body(content: Content) -> some View {
        content.alert(title, isPresented: $isPresented) {
            Button(confirmTitle, role: destructive ? .destructive : nil, action: onConfirm)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(message)
        }
    }
}

extension View {
    func vConfirm(_ title: String, message: String, confirm: String,
                  isPresented: Binding<Bool>, destructive: Bool = true,
                  onConfirm: @escaping () -> Void) -> some View {
        modifier(ConfirmDialog(title: title, message: message, confirmTitle: confirm,
                               isPresented: isPresented, destructive: destructive,
                               onConfirm: onConfirm))
    }
}

/// A `ButtonStyle` eraser, so one button builder can choose between the shared
/// styles at runtime (a primary action for a read, a secondary one for a step
/// that will ask before it acts).
struct AnyButtonStyle: ButtonStyle {
    private let make: (Configuration) -> AnyView

    init<S: ButtonStyle>(_ style: S) {
        make = { configuration in AnyView(style.makeBody(configuration: configuration)) }
    }

    func makeBody(configuration: Configuration) -> some View { make(configuration) }
}
