package com.vantage.companion.ui.devices

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
import com.vantage.companion.data.api.DeviceInfo
import com.vantage.companion.ui.components.*
import com.vantage.companion.ui.theme.*

@Composable
fun DevicesScreen(viewModel: DevicesViewModel) {
    val state by viewModel.state.collectAsState()

    Column(modifier = Modifier.fillMaxSize()) {
        if (state.error != null) {
            Notice(
                kind = NoticeKind.Error,
                message = state.error!!,
                modifier = Modifier.padding(horizontal = Space.screen, vertical = Space.s),
                actionTitle = "Dismiss",
                onAction = { viewModel.clearError() },
            )
        }

        if (state.isLoading && state.devices.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center,
            ) {
                LoadingRow("Loading paired devices…")
            }
            return
        }

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(Space.screen),
            verticalArrangement = Arrangement.spacedBy(Space.s),
        ) {
            // Pairing request: the one action this screen performs.
            item {
                Button(
                    onClick = { viewModel.requestPairing() },
                    enabled = !state.isPairing,
                    modifier = Modifier.fillMaxWidth(),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Accent,
                        contentColor = OnAccent,
                        disabledContainerColor = AccentSoft,
                        disabledContentColor = AccentDeep,
                    ),
                    shape = RoundedCornerShape(Radius.control),
                ) {
                    if (state.isPairing) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(16.dp),
                            color = AccentDeep,
                            strokeWidth = 2.dp,
                        )
                        Spacer(Modifier.width(Space.s))
                        Text("Requesting pairing…")
                    } else {
                        Icon(Icons.Filled.AddLink, contentDescription = null, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.width(Space.xs))
                        Text("Request Pairing")
                    }
                }
                Spacer(Modifier.height(Space.xs))
                Text(
                    "The laptop must approve the request before this phone is paired.",
                    color = Muted,
                    fontSize = 11.sp,
                )
            }

            // Pairing result — the server's own text, no invented success.
            if (state.pairingResult != null) {
                item {
                    Notice(
                        kind = NoticeKind.Info,
                        message = "Pairing requested: ${state.pairingResult}. " +
                            "Approve it on the laptop.",
                    )
                }
            }

            item {
                SectionHeader(
                    title = "Paired Devices",
                    modifier = Modifier.padding(top = Space.m),
                )
            }

            if (state.devices.isEmpty()) {
                item {
                    EmptyState(
                        icon = Icons.Filled.DevicesOther,
                        title = "No devices yet",
                        message = "Tap \"Request Pairing\" to ask the laptop to add this phone.",
                    )
                }
            }

            items(state.devices, key = { it.id }) { device ->
                DeviceCard(device = device)
            }
        }
    }
}

@Composable
fun DeviceCard(device: DeviceInfo) {
    // The real field is auth_state (unpaired / paired / revoked); online is a
    // separate fact and is shown as its own line rather than folded in.
    val kind = when (device.state?.lowercase()) {
        "paired" -> StatusKind.Connected
        "pending" -> StatusKind.Pending
        "revoked" -> StatusKind.Error
        else -> StatusKind.Idle
    }

    VCard {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(Space.m),
        ) {
            Icon(
                imageVector = if (device.isPhone) Icons.Filled.PhoneAndroid else Icons.Filled.Computer,
                contentDescription = null,
                tint = if (device.isHost) Accent else Muted,
                modifier = Modifier.size(22.dp),
            )
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = device.displayName,
                    fontWeight = FontWeight.SemiBold,
                    color = Ink,
                    fontSize = 14.sp,
                )
                Text(
                    text = device.deviceId?.take(20) ?: device.id.take(20),
                    color = Muted,
                    fontSize = 11.sp,
                    fontFamily = FontFamily.Monospace,
                )
            }
            StatusPill(kind, device.state ?: "unknown")
        }
        Spacer(Modifier.height(Space.s))
        Row(horizontalArrangement = Arrangement.spacedBy(Space.m)) {
            if (device.isHost) {
                Text("This laptop", color = Muted, fontSize = 10.sp)
            }
            Text(
                if (device.online) "Online" else "Offline",
                color = if (device.online) Accent else Faint,
                fontSize = 10.sp,
            )
            if (device.pairedAt != null) {
                Text(
                    text = "Paired ${device.pairedAt!!.take(10)}",
                    color = Muted,
                    fontSize = 10.sp,
                )
            }
            if (device.lastSeen != null) {
                Text(
                    text = "Seen ${device.lastSeen!!.take(10)}",
                    color = Muted,
                    fontSize = 10.sp,
                )
            }
        }
    }
}