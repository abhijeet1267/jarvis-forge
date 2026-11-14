package com.vantage.companion.ui.approvals

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

data class ApprovalsUiState(
    val isLoading: Boolean = true,
    val confirmations: List<Confirmation> = emptyList(),
    val processingIds: Set<String> = emptySet(),
    val error: String? = null,
)

class ApprovalsViewModel(
    private val api: VantageApiClient,
    private val store: SessionStore,
) : ViewModel() {

    private val _state = MutableStateFlow(ApprovalsUiState())
    val state: StateFlow<ApprovalsUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(isLoading = true)
            try {
                val confirmations = api.fetchConfirmations()
                _state.value = _state.value.copy(
                    confirmations = confirmations,
                    isLoading = false,
                    error = null,
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    isLoading = false,
                    error = e.message ?: "Could not load approvals",
                )
            }
        }
    }

    fun approve(id: String) {
        viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(
                processingIds = _state.value.processingIds + id,
            )
            try {
                api.approveConfirmation(id)
                // Remove from list
                _state.value = _state.value.copy(
                    confirmations = _state.value.confirmations.filter { it.id != id },
                    processingIds = _state.value.processingIds - id,
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    processingIds = _state.value.processingIds - id,
                    error = e.message ?: "Approval failed",
                )
            }
        }
    }

    fun deny(id: String) {
        viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(
                processingIds = _state.value.processingIds + id,
            )
            try {
                api.denyConfirmation(id)
                _state.value = _state.value.copy(
                    confirmations = _state.value.confirmations.filter { it.id != id },
                    processingIds = _state.value.processingIds - id,
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    processingIds = _state.value.processingIds - id,
                    error = e.message ?: "Deny failed",
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
            ApprovalsViewModel(api, store) as T
    }
}