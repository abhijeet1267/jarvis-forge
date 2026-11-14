package com.vantage.companion.ui.home

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.vantage.companion.ui.components.*
import com.vantage.companion.ui.theme.*

/**
 * Home: the first thing opened after pairing, so it has to answer two
 * questions at a glance — is the laptop reachable, and what is it connected to.
 *
 * It is built from the shared Vantage controls (section eyebrows, one card, one
 * status pill) rather than hand-rolled surfaces, so it cannot drift from the
 * rest of the app. Nothing here is invented: every number comes from the
 * backend and a failed read is named as a failure.
 */
@Composable
fun HomeScreen(
    viewModel: HomeViewModel,
    /** Switch to the Google tab. Supplied by the navigation host; omitted, the
     *  row is still truthful but has no button to press. */
    onOpenGoogle: (() -> Unit)? = null,
) {
    val state by viewModel.state.collectAsState()

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(Space.screen),
        verticalArrangement = Arrangement.spacedBy(Space.m),
    ) {
        // ── Backend connection ──
        item {
            SectionHeader("Connection")
        }
        item {
            VCard {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(Space.m),
                ) {
                    StatusPill(
                        kind = when {
                            state.isConnected -> StatusKind.Connected
                            state.isLoading -> StatusKind.Idle
                            else -> StatusKind.Error
                        },
                        text = when {
                            state.isConnected -> "Connected"
                            state.isLoading -> "Checking…"
                            else -> "Not connected"
                        },
                    )
                    Spacer(Modifier.weight(1f))
                    if (state.toolsCount > 0) {
                        Text(
                            "${state.toolsCount} tools",
                            color = Muted,
                            fontSize = 11.sp,
                        )
                    }
                    // Counts go stale after acting on another tab; refresh
                    // without waiting for a reconnect.
                    IconButton(
                        onClick = { viewModel.refresh() },
                        enabled = !state.isLoading,
                    ) {
                        Icon(
                            Icons.Filled.Refresh,
                            contentDescription = "Refresh connection status",
                            tint = if (state.isLoading) Faint else Accent,
                            modifier = Modifier.size(20.dp),
                        )
                    }
                }
                if (state.hostname.isNotBlank()) {
                    Spacer(Modifier.height(Space.s))
                    Text(
                        "${state.hostname} · ${state.platform}",
                        color = Muted,
                        fontSize = 12.sp,
                    )
                }
                if (state.isLoading) {
                    Spacer(Modifier.height(Space.s))
                    LoadingRow("Checking the laptop…")
                }
            }
        }

        if (state.error != null) {
            item {
                Notice(
                    kind = NoticeKind.Error,
                    message = state.error!!,
                    actionTitle = "Retry",
                    onAction = { viewModel.refresh() },
                )
            }
        }

        // ── Connections summary ──
        item {
            SectionHeader(
                title = "Connections",
                trailing = if (onOpenGoogle == null) null else {
                    {
                        TextButton(onClick = onOpenGoogle) {
                            Text("Google", color = Accent, fontSize = 12.sp)
                        }
                    }
                },
            )
        }
        item {
            ConnectionsCard(state = state)
        }

        // ── Counts ──
        item {
            SectionHeader("Overview")
        }
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(Space.m),
            ) {
                MetricCard(
                    modifier = Modifier.weight(1f),
                    label = "Pending",
                    value = state.pendingCount.toString(),
                    icon = Icons.Filled.Shield,
                    color = if (state.pendingCount > 0) Gold else Muted,
                )
                MetricCard(
                    modifier = Modifier.weight(1f),
                    label = "Devices",
                    value = state.devicesCount.toString(),
                    icon = Icons.Filled.DevicesOther,
                    color = Muted,
                )
            }
        }
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(Space.m),
            ) {
                MetricCard(
                    modifier = Modifier.weight(1f),
                    label = "Tools",
                    value = state.toolsCount.toString(),
                    icon = Icons.Filled.Build,
                    color = Accent,
                )
                MetricCard(
                    modifier = Modifier.weight(1f),
                    label = "Agents",
                    value = state.agentsCount.toString(),
                    icon = Icons.Filled.Task,
                    color = Accent,
                )
            }
        }

        // ── AI provider ──
        if (state.llmEnabled) {
            item {
                SectionHeader("AI Provider")
            }
            item {
                VCard {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(Space.m),
                    ) {
                        Icon(
                            Icons.Filled.Psychology,
                            contentDescription = null,
                            tint = Accent,
                            modifier = Modifier.size(22.dp),
                        )
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = state.aiProvider.uppercase(),
                                fontWeight = FontWeight.SemiBold,
                                color = Ink,
                                fontSize = 15.sp,
                            )
                            if (state.llmSource.isNotBlank()) {
                                Text(
                                    text = state.llmSource,
                                    color = Muted,
                                    fontSize = 11.sp,
                                )
                            }
                        }
                        StatusPill(StatusKind.Connected, "Active")
                    }
                }
            }
        }
    }
}

/**
 * One place to see what the Mac is connected to.
 *
 * Google is a real read (`/google/status`, which the phone surface is allowed
 * to reach) and its label comes from the model's `stateLabel`, so a failed
 * read is "could not read", never "not connected".
 *
 * MCP is stated, not claimed: the gateway's allowlist deliberately excludes
 * `/mcp/status`, and a refused request there also clears the session. So this
 * row names where MCP is managed (the Mac) and offers no state of its own —
 * which is the honest thing to show from a phone.
 *
 * Nothing here connects or disconnects: the connection lifecycle is
 * same-origin protected and absent from the gateway's allowlist by design.
 */
@Composable
private fun ConnectionsCard(state: HomeUiState) {
    VCard {
        Column(verticalArrangement = Arrangement.spacedBy(Space.m)) {
            ConnectionRow(
                name = "Google Calendar + Tasks",
                kind = when {
                    state.googleError != null -> StatusKind.Error
                    state.googleConnected -> StatusKind.Connected
                    state.googleSummary == null -> StatusKind.Idle
                    else -> StatusKind.Pending
                },
                label = when {
                    state.googleError != null -> "Could not read the connection"
                    else -> state.googleSummary ?: "Checking…"
                },
                detail = when {
                    state.googleError != null -> state.googleError
                    state.googleConnected ->
                        "Calendar is read-only; tasks can be read and completed."
                    else -> "Connect on the Mac — Connections in the Vantage app."
                },
            )

            HorizontalDivider(color = Line)

            ConnectionRow(
                name = "MCP server",
                kind = StatusKind.Idle,
                label = "Managed on the Mac",
                detail = "Status is not exposed to the phone; check Connections "
                    + "on the Mac for the server, its tools and the allowlist.",
            )
        }
    }
}

@Composable
private fun ConnectionRow(
    name: String,
    kind: StatusKind,
    label: String,
    detail: String,
) {
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(Space.xs),
    ) {
        Text(
            text = name,
            color = Ink,
            fontSize = 13.sp,
            fontWeight = FontWeight.Medium,
        )
        StatusPill(kind, label)
        Text(
            text = detail,
            color = Muted,
            fontSize = 11.sp,
        )
    }
}

@Composable
fun MetricCard(
    modifier: Modifier = Modifier,
    label: String,
    value: String,
    icon: ImageVector,
    color: Color,
) {
    VCard(modifier = modifier, padding = Space.l) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(Space.s),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(icon, contentDescription = null, tint = color, modifier = Modifier.size(15.dp))
            Text(text = label, color = Muted, fontSize = 12.sp)
        }
        Spacer(Modifier.height(Space.s))
        Text(
            text = value,
            color = Ink,
            fontSize = 24.sp,
            fontWeight = FontWeight.Bold,
            fontFamily = FontFamily.Monospace,
        )
    }
}