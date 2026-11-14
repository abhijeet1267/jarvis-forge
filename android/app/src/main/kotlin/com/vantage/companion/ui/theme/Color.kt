package com.vantage.companion.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/**
 * Vantage's palette on Android — the same identity as the macOS app.
 *
 * Light-first: warm white paper, ivory secondary surfaces, one emerald for
 * action and success, champagne gold as a small accent. The previous file was a
 * dark scheme (`#0D1117` background, blue `#4C8DFF` accent) that shared nothing
 * with the desktop app; the token *names* were kept on purpose, because every
 * screen already reads them semantically (`Ink` = text, `Muted` = secondary,
 * `Bad` = error). Changing the values here therefore restyles the whole app
 * without touching a screen — and a screen can never drift into its own palette.
 *
 * Deliberately scoped to light: `Theme.kt` no longer follows the system dark
 * setting, because a half-converted dark mode would be worse than a consistent
 * light one. A dark variant is a later, whole-app decision, not a per-screen one.
 */

// ── Surfaces ──────────────────────────────────────────────────────────────
/** Page background — warm white. */
val Background = Color(0xFFFAFAF7)
/** Card / panel surface — white. */
val Panel = Color(0xFFFFFFFF)
/** Secondary surface — pale ivory. */
val Ivory = Color(0xFFF4F2EA)
/** Hairline dividers. */
val Line = Color(0xFFEDEAE2)
/** Card and field outlines. */
val FieldLine = Color(0xFFE6E2D8)

// ── Text ──────────────────────────────────────────────────────────────────
/** Primary text — deep charcoal. */
val Ink = Color(0xFF252821)
/** Secondary text. */
val Muted = Color(0xFF6B6F66)
/** Tertiary text and section eyebrows. */
val Faint = Color(0xFF8E928A)

// ── Action and meaning ────────────────────────────────────────────────────
/** Primary accent: emerald. Actions, connected, success. */
val Accent = Color(0xFF16845B)
/** Pressed/deeper emerald. */
val AccentDeep = Color(0xFF116B49)
/** Emerald tint for fills and selected rows. */
val AccentSoft = Color(0xFFE7F3ED)
/** Text/icons placed on an emerald fill. */
val OnAccent = Color(0xFFFFFFFF)

/** Champagne gold — emphasis, attention, pending. Used sparingly. */
val Gold = Color(0xFFC6A45A)
/** Restrained gold border. */
val GoldBorder = Color(0xFFE4D5B2)
/** Pale gold well. */
val GoldSoft = Color(0xFFFBF6E9)

/** Accessible success (emerald, consistent with the accent). */
val Ok = Accent
val OkSoft = AccentSoft
/** Readable amber for warnings. */
val Warn = Color(0xFF97620B)
val WarnSoft = Color(0xFFFDF4E3)
/** Accessible red for errors. */
val Bad = Color(0xFFB3261E)
val BadSoft = Color(0xFFFCEDEB)

// ── Quiet controls ────────────────────────────────────────────────────────
/** Subtle hover/quiet fill. */
val GhostBg = Color(0xFFF7F6F1)
val GhostInk = Color(0xFF6B6F66)
/** Solid action fill for older call sites; the emerald accent. */
val Go = Accent

// ── Metrics ───────────────────────────────────────────────────────────────
/** Spacing scale, mirroring the macOS `V.Space` scale. */
object Space {
    val xs = 4.dp
    val s = 8.dp
    val m = 12.dp
    val l = 16.dp
    val xl = 24.dp
    val xxl = 32.dp
    val screen = 16.dp   // page side margin on a phone
}

object Radius {
    val control = 10.dp
    val card = 14.dp
    val pill = 999.dp
}