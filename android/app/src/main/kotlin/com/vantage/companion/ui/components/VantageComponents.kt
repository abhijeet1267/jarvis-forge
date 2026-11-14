package com.vantage.companion.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Pending
import androidx.compose.material.icons.filled.RadioButtonUnchecked
import androidx.compose.material.icons.filled.WarningAmber
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.vantage.companion.ui.theme.*

/**
 * Shared Android controls, mirroring the macOS design system.
 *
 * Each exists because more than one screen needs it and would otherwise drift —
 * a status colour that means four different greens, a card with its own radius,
 * a section header with its own letter-spacing. Screens compose these; a screen
 * should not add a colour of its own.
 */

// ── Page chrome ───────────────────────────────────────────────────────────

/**
 * The branded top bar. The app previously had no brand anywhere after Setup, so
 * every screen opened straight into content with nothing saying where you were.
 * This puts the Vantage mark and a page title above each screen.
 */
@Composable
fun VantageTopBar(title: String?, subtitle: String? = null) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = Space.screen, vertical = Space.m),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.m),
    ) {
        // Restrained mark: one gold ring, one emerald glyph — same as macOS.
        Box(contentAlignment = Alignment.Center) {
            Box(
                Modifier
                    .size(30.dp)
                    .border(1.5.dp, GoldBorder, CircleShape)
            )
            Icon(
                Icons.Filled.AutoAwesome,
                contentDescription = null,
                tint = Accent,
                modifier = Modifier.size(15.dp),
            )
        }
        Column(Modifier.weight(1f)) {
            Text(
                title ?: "Vantage",
                fontSize = 17.sp,
                fontWeight = FontWeight.SemiBold,
                color = Ink,
            )
            Text(
                subtitle ?: "personal AI",
                fontSize = 11.sp,
                color = Faint,
            )
        }
    }
}

/** A section eyebrow with consistent tracking. */
@Composable
fun SectionHeader(title: String, modifier: Modifier = Modifier, trailing: (@Composable () -> Unit)? = null) {
    Row(
        modifier = modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            title.uppercase(),
            fontSize = 10.sp,
            fontWeight = FontWeight.SemiBold,
            letterSpacing = 0.8.sp,
            color = Faint,
            modifier = Modifier.weight(1f),
        )
        trailing?.invoke()
    }
}

// ── Surfaces ──────────────────────────────────────────────────────────────

/** The standard card: white, 1 px outline, no shadow. */
@Composable
fun VCard(
    modifier: Modifier = Modifier,
    container: Color = Panel,
    outline: Color = FieldLine,
    padding: Dp = Space.l,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(container, RoundedCornerShape(Radius.card))
            .border(1.dp, outline, RoundedCornerShape(Radius.card))
            .padding(padding),
        content = content,
    )
}

// ── Status ────────────────────────────────────────────────────────────────

enum class StatusKind { Connected, Pending, Setup, Error, Idle }

private fun StatusKind.colour(): Color = when (this) {
    StatusKind.Connected -> Accent
    StatusKind.Pending -> Gold
    StatusKind.Setup -> Faint
    StatusKind.Error -> Bad
    StatusKind.Idle -> Faint
}

private fun StatusKind.symbol(): ImageVector = when (this) {
    StatusKind.Connected -> Icons.Filled.CheckCircle
    StatusKind.Pending -> Icons.Filled.Pending
    StatusKind.Setup -> Icons.Filled.RadioButtonUnchecked
    StatusKind.Error -> Icons.Filled.ErrorOutline
    StatusKind.Idle -> Icons.Filled.RadioButtonUnchecked
}

/** The one status pill; the dot and the word always agree. */
@Composable
fun StatusPill(kind: StatusKind, text: String) {
    val colour = kind.colour()
    Row(
        modifier = Modifier
            .background(colour.copy(alpha = 0.10f), CircleShape)
            .padding(horizontal = Space.s, vertical = 3.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Icon(kind.symbol(), contentDescription = null, tint = colour, modifier = Modifier.size(11.dp))
        Text(text, fontSize = 11.sp, fontWeight = FontWeight.Medium, color = colour)
    }
}

// ── Feedback ──────────────────────────────────────────────────────────────

enum class NoticeKind { Info, Success, Warning, Error }

private fun NoticeKind.colour(): Color = when (this) {
    NoticeKind.Info -> Muted
    NoticeKind.Success -> Accent
    NoticeKind.Warning -> Warn
    NoticeKind.Error -> Bad
}

private fun NoticeKind.symbol(): ImageVector = when (this) {
    NoticeKind.Info -> Icons.Filled.Info
    NoticeKind.Success -> Icons.Filled.CheckCircle
    NoticeKind.Warning -> Icons.Filled.WarningAmber
    NoticeKind.Error -> Icons.Filled.ErrorOutline
}

/** Inline feedback with an optional action (Retry, Dismiss, Check again). */
@Composable
fun Notice(
    kind: NoticeKind,
    message: String,
    modifier: Modifier = Modifier,
    actionTitle: String? = null,
    onAction: (() -> Unit)? = null,
) {
    val colour = kind.colour()
    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(colour.copy(alpha = 0.08f), RoundedCornerShape(Radius.control))
            .border(1.dp, colour.copy(alpha = 0.22f), RoundedCornerShape(Radius.control))
            .padding(horizontal = Space.m, vertical = Space.m),
        verticalAlignment = Alignment.Top,
        horizontalArrangement = Arrangement.spacedBy(Space.s),
    ) {
        Icon(kind.symbol(), contentDescription = null, tint = colour, modifier = Modifier.size(15.dp))
        Text(message, fontSize = 12.5.sp, color = Ink, modifier = Modifier.weight(1f))
        if (actionTitle != null && onAction != null) {
            TextButton(onClick = onAction) {
                Text(actionTitle, color = Accent, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            }
        }
    }
}

/** Empty state with optional action. */
@Composable
fun EmptyState(
    icon: ImageVector,
    title: String,
    message: String,
    actionTitle: String? = null,
    onAction: (() -> Unit)? = null,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(Space.xxl),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(Space.s),
    ) {
        Icon(icon, contentDescription = null, tint = Gold, modifier = Modifier.size(26.dp))
        Text(title, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, color = Ink)
        Text(message, fontSize = 12.5.sp, color = Muted)
        if (actionTitle != null && onAction != null) {
            Spacer(Modifier.height(Space.xs))
            Button(
                onClick = onAction,
                colors = ButtonDefaults.buttonColors(containerColor = Accent, contentColor = OnAccent),
                shape = RoundedCornerShape(Radius.control),
            ) { Text(actionTitle, fontSize = 13.sp) }
        }
    }
}

/** A quiet inline spinner with a sentence, for a real in-flight request. */
@Composable
fun LoadingRow(text: String) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.s),
    ) {
        CircularProgressIndicator(
            modifier = Modifier.size(14.dp),
            strokeWidth = 2.dp,
            color = Accent,
        )
        Text(text, fontSize = 12.5.sp, color = Muted)
    }
}

/** A key/value row: label column stays fixed so a list of rows lines up. */
@Composable
fun DetailRow(label: String, value: String, valueColor: Color = Ink, mono: Boolean = false) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(Space.m),
    ) {
        Text(label, fontSize = 12.sp, color = Muted, modifier = Modifier.width(96.dp))
        Text(
            value,
            fontSize = 12.5.sp,
            color = valueColor,
            fontFamily = if (mono) androidx.compose.ui.text.font.FontFamily.Monospace else null,
            modifier = Modifier.weight(1f),
        )
    }
}