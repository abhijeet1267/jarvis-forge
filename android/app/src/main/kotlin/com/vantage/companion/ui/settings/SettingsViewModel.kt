package com.vantage.companion.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.vantage.companion.data.api.*
import com.vantage.companion.data.local.SessionStore
import com.vantage.companion.voice.VoiceAvailability
import com.vantage.companion.voice.VoiceController
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class SettingsUiState(
    val backendUrl: String = "",
    val deviceId: String = "",
    val deviceName: String = "",
    val surface: String = "phone",
    val hasSession: Boolean = false,
    val version: String = "1.0",
    // Health info
    val isConnected: Boolean = false,
    val hostname: String = "",
    val platform: String = "",
    val llmProvider: String = "none",
    // Emergency stop
    val isStopping: Boolean = false,
    val stopResult: String? = null,
    // Sign out
    val isSigningOut: Boolean = false,
    // Dialog flags
    val showSignOutDialog: Boolean = false,
    val showStopDialog: Boolean = false,
    val showForgetDialog: Boolean = false,
    // General
    val error: String? = null,
)

class SettingsViewModel(
    private val api: VantageApiClient,
    private val store: SessionStore,
    private val voice: VoiceController? = null,
) : ViewModel() {

    private val _state = MutableStateFlow(SettingsUiState())
    val state: StateFlow<SettingsUiState> = _state.asStateFlow()

    // ── AI Voice (persisted, shared with the Command screen) ──
    val voiceEnabled: StateFlow<Boolean> = voice?.enabled ?: MutableStateFlow(false)
    val voiceAvailability: StateFlow<VoiceAvailability> =
        voice?.availability ?: MutableStateFlow(VoiceAvailability.NOT_INITIALIZED)

    fun setVoiceEnabled(enabled: Boolean) {
        voice?.setEnabled(enabled)
    }

    init {
        _state.value = _state.value.copy(
            backendUrl = store.backendUrl.value,
            deviceId = store.deviceId.value,
            deviceName = store.deviceName(),
            hasSession = store.hasSession.value,
            surface = store.surface(),
        )
        refreshHealth()
    }

    fun refreshHealth() {
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val health = api.fetchHealth()
                _state.value = _state.value.copy(
                    isConnected = health.status == "ok",
                    hostname = health.system?.hostname ?: "",
                    platform = health.system?.platform ?: "",
                    llmProvider = health.aiProvider ?: "none",
                    error = null,
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    isConnected = false,
                    error = null,
                )
            }
        }
    }

    fun showSignOutDialog() { _state.value = _state.value.copy(showSignOutDialog = true) }
    fun dismissSignOutDialog() { _state.value = _state.value.copy(showSignOutDialog = false) }

    fun signOut() {
        viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(isSigningOut = true)
            try {
                api.signOut()
            } catch (_: Exception) {
                // Fire-and-forget: clear local state regardless
            }
            store.signOut()
            _state.value = _state.value.copy(
                isSigningOut = false,
                hasSession = false,
                showSignOutDialog = false,
            )
        }
    }

    fun showStopDialog() { _state.value = _state.value.copy(showStopDialog = true) }
    fun dismissStopDialog() { _state.value = _state.value.copy(showStopDialog = false) }

    fun emergencyStop() {
        viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(isStopping = true)
            try {
                val result = api.emergencyStop()
                _state.value = _state.value.copy(
                    isStopping = false,
                    showStopDialog = false,
                    stopResult = result.detail ?: "All tasks stopped",
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    isStopping = false,
                    showStopDialog = false,
                    error = e.message ?: "Emergency stop failed",
                )
            }
        }
    }

    fun showForgetDialog() { _state.value = _state.value.copy(showForgetDialog = true) }
    fun dismissForgetDialog() { _state.value = _state.value.copy(showForgetDialog = false) }

    fun forgetDevice() {
        store.clearAll()
        _state.value = _state.value.copy(
            hasSession = false,
            showForgetDialog = false,
        )
    }

    fun clearError() {
        _state.value = _state.value.copy(error = null)
    }

    class Factory(
        private val api: VantageApiClient,
        private val store: SessionStore,
        private val voice: VoiceController? = null,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T =
            SettingsViewModel(api, store, voice) as T
    }
}