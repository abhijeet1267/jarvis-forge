package com.vantage.companion.ui.approvals

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.vantage.companion.data.api.Confirmation
import com.vantage.companion.ui.components.*
import com.vantage.companion.ui.theme.*

/**
 * Pending confirmations, as raised by the backend.
 *
 * A pending card is gold — the one thing on the screen that needs a person —
 * with the risk stated in words and the approval buttons locked while a
 * decision for that row is in flight, so one tap cannot become two requests.
 * The calls and the processing guard live in the view model; this screen only
 * draws its state.
 */
@Composable
fun ApprovalsScreen(viewModel: ApprovalsViewModel) {
    val state by viewModel.state.collectAsState()

    Column(modifier = Modifier.fillMaxSize()) {
        if (state.error != null) {
            Notice(
                kind = NoticeKind.Error,
                message = state.error!!,
                modifier = Modifier.padding(horizontal = Space.screen, vertical = Space.s),
                actionTitle = "Retry",
                onAction = {
                    viewModel.clearError()
                    viewModel.refresh()
                },
            )
        }

        if (state.isLoading && state.confirmations.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center,
            ) {
                LoadingRow("Loading approvals…")
            }
            return
        }

        if (state.confirmations.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center,
            ) {
                EmptyState(
                    icon = Icons.Filled.CheckCircle,
                    title = "No pending approvals",
                    message = "Nothing is waiting on you. Vantage asks here before " +
                        "it does anything consequential.",
                    actionTitle = "Check again",
                    onAction = { viewModel.refresh() },
                )
            }
            return
        }

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(Space.screen),
            verticalArrangement = Arrangement.spacedBy(Space.m),
        ) {
            item {
                SectionHeader(
                    title = if (state.confirmations.size == 1) "1 request waiting"
                    else "${state.confirmations.size} requests waiting",
                )
            }
            items(state.confirmations, key = { it.id }) { confirmation ->
                ConfirmationCard(
                    confirmation = confirmation,
                    isProcessing = state.processingIds.contains(confirmation.id),
                    onApprove = { viewModel.approve(confirmation.id) },
                    onDeny = { viewModel.deny(confirmation.id) },
                )
            }
        }
    }
}

@Composable
fun ConfirmationCard(
    confirmation: Confirmation,
    isProcessing: Boolean,
    onApprove: () -> Unit,
    onDeny: () -> Unit,
) {
    // Backend risk tier: 0=READ 1=LOW 2=EXTERNAL 3=DESTRUCTIVE 4=PHYSICAL.
    val riskColor = when (confirmation.riskLevel) {
        "destructive", "physical" -> Bad
        "external", "medium" -> Warn
        else -> Ok
    }

    VCard(container = GoldSoft, outline = GoldBorder) {
        // Header: action and risk
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(Space.s),
        ) {
            Text(
                text = confirmation.action,
                fontWeight = FontWeight.SemiBold,
                color = Ink,
                fontSize = 15.sp,
                modifier = Modifier.weight(1f),
            )
            StatusPill(StatusKind.Pending, "Pending")
        }

        if (!confirmation.device.isNullOrBlank()) {
            Spacer(Modifier.height(Space.s))
            Row(
                horizontalArrangement = Arrangement.spacedBy(Space.xs),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(
                    Icons.Filled.MyLocation,
                    contentDescription = null,
                    tint = Muted,
                    modifier = Modifier.size(13.dp),
                )
                Text(confirmation.device!!, color = Muted, fontSize = 12.sp)
            }
        }

        Spacer(Modifier.height(Space.s))
        Text(
            "Risk: ${confirmation.riskLevel}",
            color = riskColor,
            fontSize = 11.sp,
            fontWeight = FontWeight.SemiBold,
        )

        // Detail: the server-generated preview, then the raw parameters.
        if (!confirmation.preview.isNullOrBlank()) {
            Spacer(Modifier.height(Space.s))
            Surface(
                shape = RoundedCornerShape(Radius.control),
                color = Panel,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    text = confirmation.preview!!.take(200),
                    modifier = Modifier.padding(Space.s),
                    color = Muted,
                    fontSize = 11.sp,
                )
            }
        } else if (confirmation.params != null) {
            Spacer(Modifier.height(Space.s))
            Surface(
                shape = RoundedCornerShape(Radius.control),
                color = Panel,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    text = confirmation.params.toString().take(200),
                    modifier = Modifier.padding(Space.s),
                    color = Muted,
                    fontSize = 11.sp,
                    fontFamily = FontFamily.Monospace,
                )
            }
        }

        if (confirmation.createdAt != null) {
            Spacer(Modifier.height(Space.s))
            Text(
                "Requested ${confirmation.createdAt.take(19).replace("T", " ")}",
                color = Faint,
                fontSize = 10.sp,
            )
        }

        Spacer(Modifier.height(Space.m))

        // Actions. Both lock while this row has a decision in flight, and the
        // reason is said below them rather than left to a greyed-out button.
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(Space.s),
        ) {
            OutlinedButton(
                onClick = onDeny,
                modifier = Modifier.weight(1f),
                enabled = !isProcessing,
                colors = ButtonDefaults.outlinedButtonColors(contentColor = Bad),
                border = androidx.compose.foundation.BorderStroke(
                    width = 1.dp,
                    color = if (isProcessing) Line else Bad.copy(alpha = 0.5f),
                ),
                shape = RoundedCornerShape(Radius.control),
            ) {
                Icon(Icons.Filled.Close, contentDescription = null, modifier = Modifier.size(16.dp))
                Spacer(Modifier.width(Space.xs))
                Text("Deny", fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
            }

            Button(
                onClick = onApprove,
                modifier = Modifier.weight(1f),
                enabled = !isProcessing,
                colors = ButtonDefaults.buttonColors(
                    containerColor = Accent,
                    contentColor = OnAccent,
                    disabledContainerColor = AccentSoft,
                    disabledContentColor = AccentDeep,
                ),
                shape = RoundedCornerShape(Radius.control),
            ) {
                if (isProcessing) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(14.dp),
                        color = AccentDeep,
                        strokeWidth = 2.dp,
                    )
                    Spacer(Modifier.width(Space.xs))
                    Text("Sending…", fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                } else {
                    Icon(Icons.Filled.Check, contentDescription = null, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(Space.xs))
                    Text("Approve", fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                }
            }
        }
        if (isProcessing) {
            Spacer(Modifier.height(Space.xs))
            Text(
                "Your decision is being sent to the laptop…",
                color = Muted,
                fontSize = 11.sp,
            )
        }
    }
}