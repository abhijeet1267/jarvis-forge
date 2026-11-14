package com.vantage.companion.ui.theme

import android.app.Activity
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

/**
 * Light-first, matching the macOS app.
 *
 * The previous scheme was `darkColorScheme` with a dark-blue background and a
 * bright-blue primary — a different product visually. Light is the designed
 * appearance here too, and the status/navigation bars are told so
 * (`isAppearanceLightStatusBars = true`), otherwise white icons would sit on the
 * warm-white background and disappear.
 */
private val VantageColors = lightColorScheme(
    primary = Accent,
    onPrimary = OnAccent,
    primaryContainer = AccentSoft,
    onPrimaryContainer = AccentDeep,
    secondary = Gold,
    onSecondary = Ink,
    secondaryContainer = GoldSoft,
    onSecondaryContainer = Ink,
    tertiary = Gold,
    background = Background,
    onBackground = Ink,
    surface = Panel,
    onSurface = Ink,
    surfaceVariant = Ivory,
    onSurfaceVariant = Muted,
    outline = FieldLine,
    outlineVariant = Line,
    error = Bad,
    onError = OnAccent,
    errorContainer = BadSoft,
    onErrorContainer = Bad,
    surfaceTint = Accent,
)

@Composable
fun VantageTheme(content: @Composable () -> Unit) {
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            // Dark glyphs on the warm-white surface.
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = true
                isAppearanceLightNavigationBars = true
            }
        }
    }

    MaterialTheme(
        colorScheme = VantageColors,
        typography = VantageTypography,
        content = content,
    )
}