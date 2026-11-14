package com.vantage.companion.ui.setup

import android.os.Build
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.vantage.companion.data.api.ApiException
import com.vantage.companion.data.api.VantageApiClient
import com.vantage.companion.data.local.SessionStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import com.vantage.companion.data.api.PinnedCertificateTrustManager.MismatchException
import java.util.UUID

sealed interface SetupUiState {
    data object Idle : SetupUiState
    data object Connecting : SetupUiState
    data object Exchanging : SetupUiState
    data object Verifying : SetupUiState
    data object Done : SetupUiState
    data class Error(val message: String) : SetupUiState
}

/**
 * Pairing against the companion gateway — the only path the native app uses.
 *
 * The flow the QR code (or manual entry) drives:
 *   1. The Mac shows a pairing code + the gateway's certificate fingerprint.
 *   2. The phone learns the gateway address (deep link or manual) and the
 *      fingerprint (out-of-band — this is what makes the link safe).
 *   3. The TLS client pins that fingerprint, then redeems the one-shot code.
 *      A MITM or a wrong device fails the handshake before any request is
 *      sent; a wrong/expired/reused code is a 403 and stores nothing.
 *   4. The scoped session token is kept in the encrypted store.
 *
 * The legacy web flow (`/phone` bootstrap → `/auth/session`) is NOT used:
 * that endpoint is same-origin-protected, so a native app can never pass it —
 * the gateway redeem is the contract by design (jarvis/companion/gateway.py).
 */
class SetupViewModel(
    private val api: VantageApiClient,
    private val store: SessionStore,
) : ViewModel() {

    private val _state = MutableStateFlow<SetupUiState>(SetupUiState.Idle)
    val state: StateFlow<SetupUiState> = _state.asStateFlow()

    private val _host = MutableStateFlow("")
    val host: StateFlow<String> = _host.asStateFlow()

    private val _fingerprint = MutableStateFlow(store.certFingerprint() ?: "")
    val fingerprint: StateFlow<String> = _fingerprint.asStateFlow()

    private val _code = MutableStateFlow("")
    val code: StateFlow<String> = _code.asStateFlow()

    fun updateHost(value: String) {
        _host.value = value
    }

    fun updateFingerprint(value: String) {
        _fingerprint.value = value
    }

    fun updateCode(value: String) {
        _code.value = value
    }

    /**
     * Pre-fill from a pairing deep link (`vantage://pair?host=…&port=…&
     * fingerprint=…&code=…`) — the QR code's target. When a code is present
     * the connection is started automatically; the user watches the result.
     */
    fun applyDeepLink(host: String, port: Int?, fingerprint: String?, code: String?) {
        if (host.isNotBlank()) {
            _host.value = if (port != null) "$host:$port" else host
        }
        if (!fingerprint.isNullOrBlank()) {
            _fingerprint.value = fingerprint
        }
        if (!code.isNullOrBlank()) {
            _code.value = code
            connect()
        }
    }

    fun connect() {
        val host = _host.value.trim()
        if (host.isBlank()) {
            _state.value = SetupUiState.Error("Enter your laptop's address (from the pairing screen)")
            return
        }
        val fp = _fingerprint.value.trim()
        if (fp.isBlank()) {
            _state.value = SetupUiState.Error(
                "Enter the certificate fingerprint shown on your laptop. " +
                    "Without it the connection can't be verified."
            )
            return
        }
        val pairingCode = _code.value.trim()
        if (pairingCode.isBlank()) {
            _state.value = SetupUiState.Error("Enter the pairing code shown on your laptop")
            return
        }

        viewModelScope.launch(Dispatchers.IO) {
            try {
                // The gateway is TLS on :8766. Keep any explicit port the user
                // typed; otherwise default to the gateway, not the backend.
                val parts = host.split(":")
                val address = if (parts.size > 1) host else "${parts[0]}:8766"
                store.saveBackendURL("https://$address")
                store.saveCertFingerprint(fp)
                api.setPinnedFingerprint(fp)

                _state.value = SetupUiState.Connecting

                // The device identity is settled BEFORE the code is redeemed:
                // redeeming is where the backend learns which phone this is, so
                // the id has to exist by then. It used to be generated after,
                // which meant the backend registered the phone and never learned
                // its name.
                if (store.deviceId.value.isBlank()) {
                    val deviceId = "phone_${UUID.randomUUID().toString().take(8)}"
                    val deviceName = Build.MODEL ?: "Android"
                    store.saveDeviceIdentity(deviceId, deviceName)
                }

                _state.value = SetupUiState.Exchanging
                val session = api.redeemPairingCode(
                    pairingCode,
                    deviceId = store.deviceId.value.ifBlank { null },
                    deviceName = store.deviceName(),
                )
                val token = session.sessionToken
                if (token.isNullOrBlank()) {
                    _state.value = SetupUiState.Error(
                        "The laptop refused the pairing code. Ask for a fresh one and try again."
                    )
                    store.signOut()
                    return@launch
                }

                store.saveSession(token, session.surface ?: "phone")

                _state.value = SetupUiState.Verifying
                // Proof the session works over the pinned link: an
                // authenticated /health through the gateway. Failure throws in
                // the client and surfaces as an error — we don't claim a
                // connection we didn't see.
                val health = api.fetchHealth()
                if (health.status != "ok") {
                    _state.value = SetupUiState.Error("Connected, but the laptop reported an abnormal state")
                } else {
                    _state.value = SetupUiState.Done
                }
            } catch (e: MismatchException) {
                _state.value = SetupUiState.Error(
                    "Certificate mismatch — the device you reached is not the one you paired with " +
                        "(wrong fingerprint, or something is intercepting the connection). " +
                        "Nothing was stored. Check you entered the fingerprint from the pairing screen."
                )
                store.signOut()
            } catch (e: ApiException.Unauthorized) {
                _state.value = SetupUiState.Error("The session was rejected. Ask for a fresh pairing code.")
                store.signOut()
            } catch (e: ApiException.TokenRevoked) {
                _state.value = SetupUiState.Error("The pairing code was invalid, expired, or already used. Ask for a fresh one.")
                store.signOut()
            } catch (e: ApiException.Transport) {
                _state.value = SetupUiState.Error(
                    "Could not reach the laptop's gateway. Check that both devices are on the same " +
                        "WiFi and that Vantage is running on the Mac (pairing shown in the menu bar)."
                )
                store.signOut()
            } catch (e: ApiException) {
                _state.value = SetupUiState.Error(e.message ?: "Pairing failed")
                store.signOut()
            } catch (e: Exception) {
                _state.value = SetupUiState.Error("Pairing failed: ${e.message}")
                store.signOut()
            }
        }
    }

    class Factory(
        private val api: VantageApiClient,
        private val store: SessionStore,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T =
            SetupViewModel(api, store) as T
    }
}
