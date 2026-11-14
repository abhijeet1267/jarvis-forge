package com.vantage.companion.ui.command

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.automirrored.filled.VolumeOff
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.filled.Terminal
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.vantage.companion.ui.components.*
import com.vantage.companion.ui.theme.*
import com.vantage.companion.voice.VoiceAvailability

/**
 * The conversation with Vantage on the Mac.
 *
 * Sending goes through exactly one path — [CommandViewModel.send] — which owns
 * the in-flight guard and the voice announcement. This screen never calls the
 * API itself, so a tapped quick chip, the keyboard's send key and the send
 * button cannot race each other into two requests.
 */
@Composable
fun CommandScreen(viewModel: CommandViewModel) {
    val history by viewModel.history.collectAsState()
    val isSending by viewModel.isSending.collectAsState()
    val input by viewModel.input.collectAsState()
    val error by viewModel.error.collectAsState()
    val voiceEnabled by viewModel.voiceEnabled.collectAsState()
    val voiceAvailability by viewModel.voiceAvailability.collectAsState()
    val listState = rememberLazyListState()

    // Keep the newest message in view: the typing row follows the last entry
    // while a reply is in flight, otherwise the last entry itself. The empty
    // state occupies its own index, hence the offset.
    LaunchedEffect(history.size, isSending) {
        val offset = if (history.isEmpty()) 1 else 0
        val last = offset + history.size - if (isSending) 0 else 1
        if (last >= 0) {
            listState.animateScrollToItem(last)
        }
    }

    /**
     * Retry re-sends the last thing the person asked, through the same guarded
     * [CommandViewModel.send]. Anything they have since typed is put back in the
     * composer afterwards so a retry never silently discards a draft.
     */
    val retryLast: (() -> Unit)? = history.lastOrNull { it.role == CommandRole.USER }
        ?.let { { if (!isSending) viewModel.retry() } }

    Column(modifier = Modifier.fillMaxSize()) {
        // ── Voice toggle, above the conversation ──
        VoiceHeader(
            enabled = voiceEnabled,
            availability = voiceAvailability,
            onToggle = viewModel::toggleVoice,
        )
        HorizontalDivider(color = Line)

        if (error != null) {
            Notice(
                kind = NoticeKind.Error,
                message = error!!,
                modifier = Modifier.padding(horizontal = Space.screen, vertical = Space.s),
                actionTitle = if (retryLast != null) "Retry" else "Dismiss",
                onAction = {
                    if (retryLast != null) retryLast.invoke() else viewModel.clearError()
                },
            )
        }

        // ── Suggested prompts ──
        LazyRow(
            modifier = Modifier.fillMaxWidth(),
            contentPadding = PaddingValues(horizontal = Space.screen, vertical = Space.s),
            horizontalArrangement = Arrangement.spacedBy(Space.s),
        ) {
            items(viewModel.quickChips) { chip ->
                SuggestionChip(
                    onClick = {
                        viewModel.updateInput(chip)
                        viewModel.send()
                    },
                    enabled = !isSending,
                    label = { Text(chip, fontSize = 12.sp, maxLines = 1) },
                    shape = RoundedCornerShape(Radius.pill),
                    colors = SuggestionChipDefaults.suggestionChipColors(
                        containerColor = Panel,
                        labelColor = Muted,
                    ),
                    border = SuggestionChipDefaults.suggestionChipBorder(
                        borderColor = FieldLine,
                        enabled = !isSending,
                    ),
                )
            }
        }

        // ── Messages ──
        LazyColumn(
            state = listState,
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth(),
            contentPadding = PaddingValues(horizontal = Space.screen, vertical = Space.s),
            verticalArrangement = Arrangement.spacedBy(Space.s),
        ) {
            if (history.isEmpty()) {
                item {
                    EmptyState(
                        icon = Icons.Filled.Terminal,
                        title = "Command Center",
                        message = "Ask Vantage anything, or pick a suggestion above. " +
                            "Consequential actions come back to you for approval.",
                    )
                }
            }
            items(history, key = { it.id }) { entry ->
                CommandBubble(entry = entry)
            }
            if (isSending) {
                item {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = Space.xs),
                    ) {
                        LoadingRow("Vantage is thinking…")
                    }
                }
            }
        }

        HorizontalDivider(color = Line)

        // ── Composer ──
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(Panel)
                .padding(horizontal = Space.m, vertical = Space.s),
            horizontalArrangement = Arrangement.spacedBy(Space.s),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = input,
                onValueChange = viewModel::updateInput,
                placeholder = { Text("Type a command…", color = Faint, fontSize = 14.sp) },
                modifier = Modifier.weight(1f),
                singleLine = true,
                enabled = !isSending,
                textStyle = LocalTextStyle.current.copy(color = Ink, fontSize = 14.sp),
                shape = RoundedCornerShape(Radius.control),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(
                    onSend = {
                        if (input.isNotBlank() && !isSending) viewModel.send()
                    },
                ),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = Accent,
                    unfocusedBorderColor = FieldLine,
                    disabledBorderColor = Line,
                    cursorColor = Accent,
                ),
            )
            SendButton(
                enabled = input.isNotBlank() && !isSending,
                sending = isSending,
                onClick = { viewModel.send() },
            )
        }
    }
}

@Composable
private fun SendButton(enabled: Boolean, sending: Boolean, onClick: () -> Unit) {
    val container = if (enabled || sending) Accent else GhostBg
    val content = if (enabled || sending) OnAccent else Faint
    IconButton(
        onClick = onClick,
        enabled = enabled,
        modifier = Modifier
            .size(44.dp)
            .background(container, RoundedCornerShape(Radius.control)),
    ) {
        if (sending) {
            CircularProgressIndicator(
                modifier = Modifier.size(18.dp),
                color = OnAccent,
                strokeWidth = 2.dp,
            )
        } else {
            Icon(
                Icons.AutoMirrored.Filled.Send,
                contentDescription = "Send command",
                tint = content,
                modifier = Modifier.size(20.dp),
            )
        }
    }
}

@Composable
fun VoiceHeader(
    enabled: Boolean,
    availability: VoiceAvailability,
    onToggle: () -> Unit,
) {
    val unavailable = availability == VoiceAvailability.UNAVAILABLE
    val on = enabled && !unavailable
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(Background)
            .padding(horizontal = Space.screen, vertical = Space.s),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.s),
    ) {
        Row(
            modifier = Modifier
                .background(
                    if (on) AccentSoft else GhostBg,
                    RoundedCornerShape(Radius.pill),
                )
                .padding(start = Space.s, end = Space.xs, top = 2.dp, bottom = 2.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(Space.xs),
        ) {
            Icon(
                imageVector = if (on) Icons.AutoMirrored.Filled.VolumeUp
                else Icons.AutoMirrored.Filled.VolumeOff,
                contentDescription = null,
                tint = when {
                    unavailable -> Warn
                    on -> Accent
                    else -> Muted
                },
                modifier = Modifier.size(16.dp),
            )
            Text(
                text = commandVoiceLabel(enabled, availability),
                color = when {
                    unavailable -> Warn
                    on -> AccentDeep
                    else -> GhostInk
                },
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
            )
            Switch(
                checked = on,
                onCheckedChange = { onToggle() },
                enabled = !unavailable,
                modifier = Modifier.height(26.dp),
                colors = SwitchDefaults.colors(
                    checkedThumbColor = OnAccent,
                    checkedTrackColor = Accent,
                    uncheckedThumbColor = Faint,
                    uncheckedTrackColor = Line,
                ),
            )
        }
        if (unavailable) {
            Text(
                "Voice unavailable on this device",
                color = Warn,
                fontSize = 11.sp,
            )
        }
    }
}

/** Header label: "AI Voice ON" / "AI Voice OFF", or the honest failure. */
fun commandVoiceLabel(enabled: Boolean, availability: VoiceAvailability): String =
    when {
        availability == VoiceAvailability.UNAVAILABLE -> "AI Voice unavailable"
        enabled -> "AI Voice ON"
        else -> "AI Voice OFF"
    }

/**
 * One message. The two speakers are told apart by alignment and surface, not
 * only by colour: the person's words sit end-aligned in an emerald-tinted
 * bubble, Vantage's answers start-aligned on a white card with a hairline
 * outline. Text is Ink on both, so it stays readable at arm's length.
 */
@Composable
fun CommandBubble(entry: CommandEntry) {
    val isUser = entry.role == CommandRole.USER
    val isError = entry.role == CommandRole.ERROR
    val shape = RoundedCornerShape(Radius.card)

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (isUser) Alignment.End else Alignment.Start,
    ) {
        Column(
            modifier = Modifier
                .widthIn(max = 320.dp)
                .background(
                    when {
                        isError -> BadSoft
                        isUser -> AccentSoft
                        else -> Panel
                    },
                    shape,
                )
                .border(
                    width = 1.dp,
                    color = when {
                        isError -> Bad.copy(alpha = 0.35f)
                        isUser -> AccentSoft
                        else -> FieldLine
                    },
                    shape = shape,
                )
                .padding(horizontal = Space.m, vertical = 10.dp),
        ) {
            Text(
                text = entry.text,
                color = Ink,
                fontSize = 14.sp,
                lineHeight = 20.sp,
            )
        }

        if (entry.confirmationRequired || entry.verified) {
            Spacer(Modifier.height(Space.xs))
            Row(horizontalArrangement = Arrangement.spacedBy(Space.xs)) {
                if (entry.confirmationRequired) {
                    StatusPill(StatusKind.Pending, "Requires approval")
                }
                if (entry.verified) {
                    StatusPill(StatusKind.Connected, "Verified")
                }
            }
        }
    }
}