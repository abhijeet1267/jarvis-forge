package com.vantage.companion.ui.devices

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.vantage.companion.data.api.*
import com.vantage.companion.data.local.SessionStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class DevicesUiState(
    val isLoading: Boolean = true,
    val devices: List<DeviceInfo> = emptyList(),
    val isPairing: Boolean = false,
    val pairingResult: String? = null,
    val error: String? = null,
)

class DevicesViewModel(
    private val api: VantageApiClient,
    private val store: SessionStore,
) : ViewModel() {

    private val _state = MutableStateFlow(DevicesUiState())
    val state: StateFlow<DevicesUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(isLoading = true)
            try {
                val devices = api.fetchDevices()
                _state.value = _state.value.copy(
                    devices = devices,
                    isLoading = false,
                    error = null,
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    isLoading = false,
                    error = e.message ?: "Could not load devices",
                )
            }
        }
    }

    fun requestPairing() {
        val deviceId = store.deviceId.value
        val deviceName = store.deviceName()
        if (deviceId.isBlank() || deviceName.isBlank()) {
            _state.value = _state.value.copy(error = "Device identity not set")
            return
        }

        viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(isPairing = true)
            try {
                val result = api.requestPairing(deviceId, deviceName)
                _state.value = _state.value.copy(
                    isPairing = false,
                    pairingResult = result.pairingId ?: "Pairing requested",
                    error = null,
                )
                refresh()
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    isPairing = false,
                    error = e.message ?: "Pairing request failed",
                )
            }
        }
    }

    fun clearError() {
        _state.value = _state.value.copy(error = null)
    }

    class Factory(
        private val api: VantageApiClient,
        private val store: SessionStore,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T =
            DevicesViewModel(api, store) as T
    }
}