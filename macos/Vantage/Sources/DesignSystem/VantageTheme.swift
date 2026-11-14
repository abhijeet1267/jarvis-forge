import SwiftUI
import AppKit

/// Vantage's visual language: warm-white, ivory, emerald and champagne gold.
///
/// Light is the designed appearance. Every colour is a *dynamic* NSColor pair, so
/// the app still behaves in dark mode instead of forcing one scheme — the earlier
/// build pinned `.preferredColorScheme(.dark)`, which is why a light-first design
/// could never have shown. Dark values are derived, not invented per screen, so a
/// screen cannot drift into its own palette.
///
/// Rules this file exists to enforce:
/// - one accent for primary action and success (emerald), one for emphasis (gold);
/// - gold is a *border and small accent*, never a large fill;
/// - text hierarchy is three levels, not ad-hoc opacity values;
/// - spacing and radii come from the scale, so screens line up without guessing.
enum V {
    // MARK: - Colour tokens

    static let background = Color(nsColor: .vBackground)
    static let surface = Color(nsColor: .vSurface)
    static let surfaceAlt = Color(nsColor: .vSurfaceAlt)
    static let sidebar = Color(nsColor: .vSidebar)

    static let text = Color(nsColor: .vText)
    static let textSecondary = Color(nsColor: .vTextSecondary)
    static let textTertiary = Color(nsColor: .vTextTertiary)

    static let accent = Color(nsColor: .vAccent)            // emerald
    static let accentPressed = Color(nsColor: .vAccentPressed)
    static let accentSoft = Color(nsColor: .vAccentSoft)    // emerald tint for fills

    static let gold = Color(nsColor: .vGold)                // champagne
    static let goldBorder = Color(nsColor: .vGoldBorder)
    static let goldSoft = Color(nsColor: .vGoldSoft)

    static let border = Color(nsColor: .vBorder)
    static let divider = Color(nsColor: .vDivider)
    static let fill = Color(nsColor: .vFill)                // subtle hover/quiet fill

    static let danger = Color(nsColor: .vDanger)
    static let dangerSoft = Color(nsColor: .vDangerSoft)
    static let warning = Color(nsColor: .vWarning)
    static let warningSoft = Color(nsColor: .vWarningSoft)
    static let success = accent
    static let successSoft = accentSoft

    // MARK: - Metrics

    enum Space {
        static let xxs: CGFloat = 2
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
        static let xxl: CGFloat = 32
        static let gutter: CGFloat = 28      // page side margin
        static let section: CGFloat = 24     // between sections
    }

    enum Radius {
        static let control: CGFloat = 7
        static let card: CGFloat = 12
        static let pill: CGFloat = 999
    }

    enum Font {
        static let pageTitle = SwiftUI.Font.system(size: 24, weight: .semibold, design: .default)
        static let sectionTitle = SwiftUI.Font.system(size: 13, weight: .semibold)
        static let cardTitle = SwiftUI.Font.system(size: 14, weight: .semibold)
        static let body = SwiftUI.Font.system(size: 13)
        static let message = SwiftUI.Font.system(size: 13.5)
        static let label = SwiftUI.Font.system(size: 12, weight: .medium)
        static let caption = SwiftUI.Font.system(size: 11)
        static let mono = SwiftUI.Font.system(size: 11, design: .monospaced)
    }

    // MARK: - Motion

    /// One place that decides whether motion runs.
    ///
    /// `NSWorkspace.accessibilityDisplayShouldReduceMotion` is the same switch
    /// System Settings exposes, so an accessibility user gets a still interface
    /// rather than a slower one. Callers pass this into `withAnimation`; the
    /// helpers below make the reduced case a no-op instead of a shortcut.
    static var motionAllowed: Bool {
        !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    static let quick: Animation = .easeOut(duration: 0.16)
    static let standard: Animation = .easeInOut(duration: 0.22)

    static func animate<Result>(_ animation: Animation = quick,
                                _ body: () throws -> Result) rethrows -> Result {
        guard motionAllowed else { return try body() }
        return try withAnimation(animation, body)
    }
}

// MARK: - Dynamic colour definitions

extension NSColor {
    fileprivate static func pair(light: String, dark: String) -> NSColor {
        NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return NSColor(hex: isDark ? dark : light)
        }
    }

    static let vBackground = pair(light: "FAFAF7", dark: "17181A")
    static let vSurface = pair(light: "FFFFFF", dark: "1F2124")
    static let vSurfaceAlt = pair(light: "F4F2EA", dark: "25272B")
    static let vSidebar = pair(light: "F2F0E8", dark: "1B1D20")

    static let vText = pair(light: "252821", dark: "ECEDE8")
    static let vTextSecondary = pair(light: "6B6F66", dark: "A7AAA2")
    static let vTextTertiary = pair(light: "8E928A", dark: "7E827A")

    static let vAccent = pair(light: "16845B", dark: "3FBF8A")
    static let vAccentPressed = pair(light: "116B49", dark: "34A576")
    static let vAccentSoft = pair(light: "E7F3ED", dark: "1E3A2E")

    static let vGold = pair(light: "C6A45A", dark: "D9BC7C")
    static let vGoldBorder = pair(light: "E4D5B2", dark: "4A4130")
    static let vGoldSoft = pair(light: "FBF6E9", dark: "2E2A20")

    static let vBorder = pair(light: "E6E2D8", dark: "34373C")
    static let vDivider = pair(light: "EDEAE2", dark: "2E3136")
    static let vFill = pair(light: "F7F6F1", dark: "26282C")

    static let vDanger = pair(light: "B3261E", dark: "F2938C")
    static let vDangerSoft = pair(light: "FCEDEB", dark: "3A2220")
    static let vWarning = pair(light: "97620B", dark: "E0B05A")
    static let vWarningSoft = pair(light: "FDF4E3", dark: "37301F")
}

extension NSColor {
    /// `#RRGGBB` or `RRGGBB`. A malformed value falls back to the text colour
    /// rather than crashing — a theme typo must not take the app down.
    convenience init(hex: String) {
        var value = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        if value.count == 3 { value = value.map { "\($0)\($0)" }.joined() }
        guard value.count == 6, let rgb = UInt32(value, radix: 16) else {
            self.init(calibratedWhite: 0.5, alpha: 1)
            return
        }
        self.init(
            srgbRed: CGFloat((rgb >> 16) & 0xFF) / 255,
            green: CGFloat((rgb >> 8) & 0xFF) / 255,
            blue: CGFloat(rgb & 0xFF) / 255,
            alpha: 1)
    }
}