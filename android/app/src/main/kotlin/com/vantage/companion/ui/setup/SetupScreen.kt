package com.vantage.companion.ui.setup

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.filled.Key
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Shield
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.vantage.companion.ui.components.*
import com.vantage.companion.ui.theme.*

/**
 * First run: pair this phone with the Mac.
 *
 * Setup is drawn outside the navigation host (it is what leads *into* it), so
 * it carries its own brand lockup instead of the shared top bar. The pairing
 * flow itself — pin the fingerprint, redeem the one-shot code — is unchanged;
 * only the surfaces are the shared card and notice controls.
 */
@Composable
fun SetupScreen(viewModel: SetupViewModel) {
    val state by viewModel.state.collectAsState()
    val host by viewModel.host.collectAsState()
    val fingerprint by viewModel.fingerprint.collectAsState()
    val code by viewModel.code.collectAsState()

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Background),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            modifier = Modifier
                .verticalScroll(rememberScrollState())
                .widthIn(max = 400.dp)
                .padding(Space.xl),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(Space.l),
        ) {
            // Brand lockup
            Box(
                modifier = Modifier
                    .size(64.dp)
                    .clip(RoundedCornerShape(Radius.card))
                    .background(AccentSoft),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    imageVector = Icons.Filled.Shield,
                    contentDescription = null,
                    tint = Accent,
                    modifier = Modifier.size(34.dp),
                )
            }

            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(
                    text = "Vantage",
                    fontSize = 26.sp,
                    fontWeight = FontWeight.Bold,
                    color = Ink,
                )
                Text(
                    text = "Pair with your laptop",
                    fontSize = 13.sp,
                    color = Muted,
                )
            }

            Text(
                text = "Open Vantage on the Mac, show the pairing code, then enter it " +
                    "below along with the certificate fingerprint. The connection is " +
                    "verified against that fingerprint before anything is sent.",
                fontSize = 12.sp,
                color = Muted,
                textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth(),
            )

            VCard {
                // Gateway address
                OutlinedTextField(
                    value = host,
                    onValueChange = viewModel::updateHost,
                    label = { Text("Laptop address") },
                    placeholder = { Text("192.168.1.50:8766") },
                    singleLine = true,
                    enabled = !isBusy(state),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
                    modifier = Modifier.fillMaxWidth(),
                    colors = fieldColors(),
                )
                Spacer(Modifier.height(Space.m))

                // Certificate fingerprint (what makes the link verifiable)
                OutlinedTextField(
                    value = fingerprint,
                    onValueChange = viewModel::updateFingerprint,
                    label = { Text("Certificate fingerprint") },
                    placeholder = { Text("3DF3 CB13 C0F3 …") },
                    singleLine = true,
                    enabled = !isBusy(state),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
                    modifier = Modifier.fillMaxWidth(),
                    colors = fieldColors(),
                )
                Spacer(Modifier.height(Space.m))

                // One-shot pairing code
                OutlinedTextField(
                    value = code,
                    onValueChange = viewModel::updateCode,
                    label = { Text("Pairing code") },
                    placeholder = { Text("a1b2c3d4e5f6…") },
                    singleLine = true,
                    enabled = !isBusy(state),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                    leadingIcon = {
                        Icon(Icons.Filled.Key, contentDescription = null, tint = Muted)
                    },
                    modifier = Modifier.fillMaxWidth(),
                    colors = fieldColors(),
                )
            }

            // Connect
            Button(
                onClick = viewModel::connect,
                enabled = host.isNotBlank() && fingerprint.isNotBlank() &&
                        code.isNotBlank() && !isBusy(state),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(50.dp),
                shape = RoundedCornerShape(Radius.control),
                colors = ButtonDefaults.buttonColors(
                    containerColor = Accent,
                    contentColor = OnAccent,
                    disabledContainerColor = AccentSoft,
                    disabledContentColor = AccentDeep,
                ),
            ) {
                if (isBusy(state)) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(18.dp),
                        color = AccentDeep,
                        strokeWidth = 2.dp,
                    )
                    Spacer(Modifier.width(Space.s))
                }
                Text(
                    text = buttonLabel(state),
                    fontWeight = FontWeight.SemiBold,
                )
                if (!isBusy(state)) {
                    Spacer(Modifier.width(Space.s))
                    Icon(Icons.AutoMirrored.Filled.ArrowForward, contentDescription = null, modifier = Modifier.size(18.dp))
                }
            }

            // Why the button is disabled, so it is never a mystery.
            if (!isBusy(state) && (host.isBlank() || fingerprint.isBlank() || code.isBlank())) {
                Text(
                    "Enter the laptop address, its certificate fingerprint and the " +
                        "pairing code to connect.",
                    fontSize = 11.sp,
                    color = Faint,
                    textAlign = TextAlign.Center,
                )
            }

            // Status
            when (val s = state) {
                is SetupUiState.Idle -> {}
                is SetupUiState.Connecting -> LoadingRow("Verifying the laptop's identity…")
                is SetupUiState.Exchanging -> LoadingRow("Exchanging the pairing code…")
                is SetupUiState.Verifying -> LoadingRow("Checking the connection…")
                is SetupUiState.Done -> Notice(
                    kind = NoticeKind.Success,
                    message = "Paired — the session is stored securely on this phone.",
                )
                is SetupUiState.Error -> Notice(
                    kind = NoticeKind.Error,
                    message = s.message,
                )
            }

            Row(
                horizontalArrangement = Arrangement.spacedBy(Space.xs),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(
                    Icons.Filled.Lock,
                    contentDescription = null,
                    tint = Faint,
                    modifier = Modifier.size(12.dp),
                )
                Text(
                    "The certificate is pinned; an impostor fails before any data is sent.",
                    fontSize = 10.sp,
                    color = Faint,
                )
            }
        }
    }
}

private fun isBusy(state: SetupUiState): Boolean =
    state is SetupUiState.Connecting ||
        state is SetupUiState.Exchanging ||
        state is SetupUiState.Verifying

@Composable
private fun fieldColors() = OutlinedTextFieldDefaults.colors(
    focusedBorderColor = Accent,
    unfocusedBorderColor = FieldLine,
    disabledBorderColor = Line,
    focusedLabelColor = Accent,
    unfocusedLabelColor = Muted,
    focusedTextColor = Ink,
    unfocusedTextColor = Ink,
    cursorColor = Accent,
    disabledTextColor = Muted,
)

private fun buttonLabel(state: SetupUiState): String = when (state) {
    is SetupUiState.Connecting -> "Verifying…"
    is SetupUiState.Exchanging -> "Pairing…"
    is SetupUiState.Verifying -> "Checking…"
    else -> "Connect"
}