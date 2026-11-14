package com.vantage.companion.ui.settings

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Logout
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.vantage.companion.ui.components.*
import com.vantage.companion.ui.theme.*
import com.vantage.companion.voice.VoiceAvailability

@Composable
fun SettingsScreen(viewModel: SettingsViewModel) {
    val state by viewModel.state.collectAsState()
    val voiceEnabled by viewModel.voiceEnabled.collectAsState()
    val voiceAvailability by viewModel.voiceAvailability.collectAsState()

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(Space.screen),
        verticalArrangement = Arrangement.spacedBy(Space.s),
    ) {
        // AI Voice — deliberately first: the most visible control on the screen.
        item {
            SectionHeader(title = "AI Voice", modifier = Modifier.padding(top = Space.xs))
        }
        item {
            VoiceSettingsCard(
                enabled = voiceEnabled,
                availability = voiceAvailability,
                onChange = viewModel::setVoiceEnabled,
            )
        }

        if (state.error != null) {
            item {
                Notice(
                    kind = NoticeKind.Error,
                    message = state.error!!,
                    modifier = Modifier.padding(top = Space.m),
                )
            }
        }

        // Connection section
        item {
            SectionHeader(title = "Connection", modifier = Modifier.padding(top = Space.m))
        }
        item {
            VCard {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        "Backend status",
                        color = Ink,
                        fontSize = 13.sp,
                        modifier = Modifier.weight(1f),
                    )
                    StatusPill(
                        kind = if (state.isConnected) StatusKind.Connected else StatusKind.Error,
                        text = if (state.isConnected) "Connected" else "Not connected",
                    )
                }
                HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = Space.m))
                SettingRow(
                    icon = Icons.Filled.Link,
                    label = "Backend URL",
                    value = state.backendUrl.ifBlank { "Not configured" },
                )
                if (state.hostname.isNotBlank()) {
                    HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = Space.m))
                    SettingRow(
                        icon = Icons.Filled.Computer,
                        label = "Host",
                        value = "${state.hostname} · ${state.platform}",
                    )
                }
                HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = Space.m))
                SettingRow(
                    icon = Icons.Filled.Psychology,
                    label = "AI Provider",
                    value = state.llmProvider.uppercase(),
                )
                HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = Space.m))
                SettingRow(
                    icon = Icons.Filled.DevicesOther,
                    label = "Surface",
                    value = state.surface,
                )
            }
        }

        // Device Identity section
        item {
            SectionHeader(title = "Device Identity", modifier = Modifier.padding(top = Space.m))
        }
        item {
            VCard {
                SettingRow(
                    icon = Icons.Filled.Tag,
                    label = "Device ID",
                    value = state.deviceId.ifBlank { "Not set" },
                )
                HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = Space.m))
                SettingRow(
                    icon = Icons.Filled.PhoneAndroid,
                    label = "Device Name",
                    value = state.deviceName.ifBlank { "Unknown" },
                )
            }
        }

        // Actions section
        item {
            SectionHeader(title = "Actions", modifier = Modifier.padding(top = Space.m))
        }
        item {
            Button(
                onClick = { viewModel.showStopDialog() },
                enabled = !state.isStopping,
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(
                    containerColor = Bad,
                    contentColor = OnAccent,
                    disabledContainerColor = BadSoft,
                    disabledContentColor = Bad,
                ),
                shape = RoundedCornerShape(Radius.control),
            ) {
                if (state.isStopping) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(16.dp),
                        color = Bad,
                        strokeWidth = 2.dp,
                    )
                    Spacer(modifier = Modifier.width(Space.s))
                }
                Icon(Icons.Filled.Stop, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(Space.xs))
                Text("Emergency Stop")
            }
            if (state.isStopping) {
                Spacer(Modifier.height(Space.xs))
                Text(
                    "Stopping all running tasks on the laptop…",
                    color = Muted,
                    fontSize = 11.sp,
                )
            }
        }

        // Stop result
        if (state.stopResult != null) {
            item {
                Notice(
                    kind = NoticeKind.Success,
                    message = state.stopResult!!,
                )
            }
        }

        // Sign Out
        item {
            OutlinedButton(
                onClick = { viewModel.showSignOutDialog() },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = Bad),
                shape = RoundedCornerShape(Radius.control),
            ) {
                Icon(Icons.AutoMirrored.Filled.Logout, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(Space.xs))
                Text("Sign Out")
            }
        }

        // About section
        item {
            SectionHeader(title = "About", modifier = Modifier.padding(top = Space.m))
        }
        item {
            VCard {
                SettingRow(
                    icon = Icons.Filled.Info,
                    label = "Version",
                    value = state.version,
                )
                HorizontalDivider(color = Line, modifier = Modifier.padding(vertical = Space.m))
                SettingRow(
                    icon = Icons.Filled.Hub,
                    label = "Backend",
                    value = if (state.hostname.isNotBlank()) state.hostname else "unknown",
                )
            }
        }

        // Danger Zone
        item {
            SectionHeader(title = "Danger Zone", modifier = Modifier.padding(top = Space.m))
        }
        item {
            OutlinedButton(
                onClick = { viewModel.showForgetDialog() },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = Bad),
                shape = RoundedCornerShape(Radius.control),
            ) {
                Icon(Icons.Filled.DeleteForever, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(Space.xs))
                Text("Forget This Device")
            }
        }
    }

    // Dialogs
    if (state.showSignOutDialog) {
        AlertDialog(
            onDismissRequest = { viewModel.dismissSignOutDialog() },
            title = { Text("Sign Out", color = Ink) },
            text = { Text("Your session will end. You'll need to pair again to reconnect.", color = Muted) },
            confirmButton = {
                TextButton(onClick = { viewModel.signOut() }) {
                    Text("Sign Out", color = Bad)
                }
            },
            dismissButton = {
                TextButton(onClick = { viewModel.dismissSignOutDialog() }) {
                    Text("Cancel", color = Muted)
                }
            },
            containerColor = Panel,
        )
    }

    if (state.showStopDialog) {
        AlertDialog(
            onDismissRequest = { viewModel.dismissStopDialog() },
            title = { Text("Emergency Stop", color = Bad) },
            text = { Text("This will immediately stop all running tasks on the laptop. The laptop may need manual intervention to resume.", color = Ink) },
            confirmButton = {
                TextButton(onClick = { viewModel.emergencyStop() }) {
                    Text("Stop Everything", color = Bad)
                }
            },
            dismissButton = {
                TextButton(onClick = { viewModel.dismissStopDialog() }) {
                    Text("Cancel", color = Muted)
                }
            },
            containerColor = Panel,
        )
    }

    if (state.showForgetDialog) {
        AlertDialog(
            onDismissRequest = { viewModel.dismissForgetDialog() },
            title = { Text("Forget Device", color = Bad) },
            text = { Text("This will erase all local session data and device identity. You'll need to pair again from scratch.", color = Ink) },
            confirmButton = {
                TextButton(onClick = { viewModel.forgetDevice() }) {
                    Text("Forget", color = Bad)
                }
            },
            dismissButton = {
                TextButton(onClick = { viewModel.dismissForgetDialog() }) {
                    Text("Cancel", color = Muted)
                }
            },
            containerColor = Panel,
        )
    }
}

@Composable
fun VoiceSettingsCard(
    enabled: Boolean,
    availability: VoiceAvailability,
    onChange: (Boolean) -> Unit,
) {
    val unavailable = availability == VoiceAvailability.UNAVAILABLE
    val on = enabled && !unavailable
    VCard {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                Icons.Filled.RecordVoiceOver,
                contentDescription = null,
                tint = if (on) Accent else Muted,
                modifier = Modifier.size(20.dp),
            )
            Spacer(Modifier.width(Space.m))
            Column(Modifier.weight(1f)) {
                Text(
                    text = "AI Voice Responses",
                    color = Ink,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Medium,
                )
                Spacer(Modifier.height(2.dp))
                Text(
                    text = voiceStateSentence(enabled, availability),
                    color = if (unavailable) Warn else Muted,
                    fontSize = 12.sp,
                )
            }
            Switch(
                checked = on,
                onCheckedChange = onChange,
                enabled = !unavailable,
                // A bare switch announces "on"/"off" with no subject. The label
                // and the actual state are what a screen-reader user needs.
                modifier = Modifier.semantics {
                    contentDescription = "AI Voice Responses"
                    stateDescription = when {
                        unavailable -> "Unavailable on this device"
                        on -> "On — replies are read aloud"
                        else -> "Off — replies stay text-only"
                    }
                },
                colors = SwitchDefaults.colors(
                    checkedThumbColor = OnAccent,
                    checkedTrackColor = Accent,
                    uncheckedThumbColor = Faint,
                    uncheckedTrackColor = Line,
                ),
            )
        }
        if (unavailable) {
            Spacer(Modifier.height(Space.s))
            Notice(
                kind = NoticeKind.Warning,
                message = "The speech engine is not installed or not available on this " +
                    "device, so replies stay text-only. Text controls are unaffected.",
            )
        }
    }
}

/**
 * The honest state sentence under the switch. When the engine failed we say
 * so instead of showing a switch that could never produce sound.
 */
fun voiceStateSentence(enabled: Boolean, availability: VoiceAvailability): String =
    when {
        availability == VoiceAvailability.UNAVAILABLE -> "Voice unavailable on this device"
        enabled -> "On — Vantage reads its replies aloud"
        else -> "Off — replies stay text-only"
    }

@Composable
fun SettingRow(
    icon: ImageVector,
    label: String,
    value: String,
    valueColor: androidx.compose.ui.graphics.Color = Muted,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(Space.m),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(icon, contentDescription = null, tint = Muted, modifier = Modifier.size(16.dp))
        Text(
            text = label,
            color = Ink,
            fontSize = 13.sp,
            modifier = Modifier.weight(1f),
        )
        Text(
            text = value,
            color = valueColor,
            fontSize = 12.sp,
            fontFamily = FontFamily.Monospace,
            fontWeight = FontWeight.Medium,
        )
    }
}