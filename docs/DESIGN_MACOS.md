# macOS design direction

The native SwiftUI app (`com.jarvisforge.vantage`) is the product. This file
records the visual system it is built on, so a future screen inherits the design
instead of inventing a fourth shade of grey.

## The look

Light-first, calm, and expensive rather than loud: warm white paper, ivory
secondary surfaces, one emerald for action and success, champagne gold as a
small accent and border. No dark-blue gradients, no glass effects, no gold-filled
cards. If a screen needs attention, it earns it with a gold _border_ or an
emerald _fill on the one primary button_ — never both at once.

| Token                     | Value (light)         | Used for                           |
| ------------------------- | --------------------- | ---------------------------------- |
| `V.background`            | `#FAFAF7`             | Page background                    |
| `V.surface`               | `#FFFFFF`             | Cards, fields, composer            |
| `V.surfaceAlt`            | `#F4F2EA`             | Ivory cards, quiet wells           |
| `V.sidebar`               | `#F2F0E8`             | Navigation rail                    |
| `V.text`                  | `#252821`             | Primary text                       |
| `V.textSecondary`         | `#6B6F66`             | Labels, descriptions               |
| `V.textTertiary`          | `#8E928A`             | Captions, section eyebrows         |
| `V.accent`                | `#16845B`             | Primary action, connected, success |
| `V.accentPressed`         | `#116B49`             | Pressed emerald                    |
| `V.gold` / `V.goldBorder` | `#C6A45A` / `#E4D5B2` | Accent, restrained borders         |
| `V.border` / `V.divider`  | `#E6E2D8` / `#EDEAE2` | Card outlines, separators          |
| `V.danger`                | `#B3261E`             | Errors, destructive actions        |
| `V.warning`               | `#97620B`             | Warnings, needs-attention          |

Every token is a **dynamic pair**: each has a derived dark value, so the app
still works when the system is dark. The earlier build forced
`.preferredColorScheme(.dark)`, which is why a light-first design could never
have appeared; that override is gone.

## Structure

```
Sources/DesignSystem/VantageTheme.swift       tokens, metrics, motion policy
Sources/DesignSystem/VantageComponents.swift  Card, StatusPill, Notice, buttons, fields
Sources/Views/ContentView.swift               sidebar, page shell, transitions
```

Screens compose from these. A screen may add layout, but it should not add a
colour, a radius, or a second button style.

## Motion

Short and purposeful: 160 ms ease-out for hover and press, 220 ms ease-in-out for
state changes, a crossfade on navigation, a subtle scale on primary buttons.
`V.motionAllowed` reads `NSWorkspace.accessibilityDisplayShouldReduceMotion`, and
`V.animate { }` becomes a plain mutation when it is on — an accessibility user
gets a _still_ interface, not a slower one. No repeating decorative animation.

## Performance rules the screens follow

- No network or disk work on the main thread; loads run in `Task` with their own
  in-flight state, and the view body only reads already-published values.
- No `ObservableObject` is constructed inside a view body; shared services are
  observed, not owned.
- No heavy blur or large shadow stacks. Cards use a 1 px border and a flat fill.
- Lists use stable identifiers so a state change animates the row, not the page.

## Dependencies

**None added.** SwiftUI provides everything used here: `@FocusState` for field
focus, `ButtonStyle` for press/hover states, `.animation` with an explicit
`Animation?` for reduced motion, and native `Toggle`/`TextField`/`ProgressView`
under custom chrome. LottieUI and similar were considered for animated loading
and rejected: the app needs a spinner, not a vector asset pipeline, and adding a
dependency here would raise the build's cost and platform surface for no visible
gain.
