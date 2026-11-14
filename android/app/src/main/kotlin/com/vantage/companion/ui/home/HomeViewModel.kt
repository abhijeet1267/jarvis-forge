package com.vantage.companion.ui.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.vantage.companion.data.api.*
import com.vantage.companion.data.local.SessionStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

data class HomeUiState(
    val isLoading: Boolean = true,
    val isConnected: Boolean = false,
    val status: String = "Checking...",
    val hostname: String = "",
    val platform: String = "",
    val version: String = "",
    val toolsCount: Int = 0,
    val pendingCount: Int = 0,
    val devicesCount: Int = 0,
    val llmEnabled: Boolean = false,
    val aiProvider: String = "none",
    val llmSource: String = "",
    // The backend's /health carries `agents` (e.g. ["laptop","memory","phone"])
    // and no task count at all; the home grid used to show a hardcoded 0.
    val agentsCount: Int = 0,
    val error: String? = null,
    // -- Connections summary ------------------------------------------------
    // Google is read from /google/status, which the phone surface is allowed to
    // reach. The label comes from GoogleConnection.stateLabel, which never says
    // "connected" on an unknown or unverified state.
    /** Null means the status was not read (or the read failed); see [googleError]. */
    val googleSummary: String? = null,
    val googleAccount: String = "",
    val googleConnected: Boolean = false,
    /** The Google status read itself failed: "not connected" is not the answer. */
    val googleError: String? = null,
    // -- Backend identity ---------------------------------------------------
    // `/health` answers 200 for *any* Vantage backend. To say whether the one
    // answering is current, the phone asks /health/identity too: a null result
    // means an older backend that cannot report itself, which is a different
    // fact from a network failure and must not read as healthy.
    /** Short build line, e.g. "0.1.0 · aa1b66a+dirty". */
    val backendBuild: String = "",
    /** True when the backend reports its own source is newer than the process. */
    val backendStale: Boolean = false,
    /** The backend answered health but has no identity route (an older build). */
    val backendLegacy: Boolean = false,
)

class HomeViewModel(
    private val api: VantageApiClient,
    private val store: SessionStore,
) : ViewModel() {

    private val _state = MutableStateFlow(HomeUiState())
    val state: StateFlow<HomeUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(isLoading = true)
            loadHealth()
            loadPending()
            loadDevices()
            loadGoogle()
            _state.value = _state.value.copy(isLoading = false)
        }
    }

    private suspend fun loadHealth() {
        val identity = api.fetchBackendIdentityOrNull()
        try {
            val health = api.fetchHealth()
            val ident = identity?.identity
            _state.value = _state.value.copy(
                isConnected = health.status == "ok",
                status = health.status,
                hostname = health.system?.hostname ?: "",
                platform = health.system?.platform ?: "",
                version = health.system?.version ?: "",
                toolsCount = health.tools,
                llmEnabled = health.llmEnabled,
                aiProvider = health.aiProvider ?: "none",
                llmSource = health.llmSource ?: "",
                agentsCount = health.agents.size,
                backendBuild = listOfNotNull(ident?.appVersion, ident?.revision)
                    .filter { it.isNotBlank() }.joinToString(" · "),
                backendStale = identity?.code?.stale == true,
                backendLegacy = identity == null,
                error = null,
            )
        } catch (e: Exception) {
            _state.value = _state.value.copy(
                isConnected = false,
                status = "unreachable",
                error = when (e) {
                    is ApiException.Transport -> "Could not reach the laptop"
                    is ApiException.Unauthorized -> "Session expired — go to Settings to reconnect"
                    else -> e.message ?: "Connection failed"
                },
            )
        }
    }

    private suspend fun loadPending() {
        try {
            val confirmations = api.fetchConfirmations()
            _state.value = _state.value.copy(pendingCount = confirmations.size)
        } catch (_: Exception) {
            // Non-fatal — leave pending count as is
        }
    }

    private suspend fun loadDevices() {
        try {
            _state.value = _state.value.copy(devicesCount = api.fetchDevices().size)
        } catch (_: Exception) {
            // Non-fatal — the connection error is already reported by loadHealth
        }
    }

    /**
     * The Google connection, for the Home summary.
     *
     * A failed read is recorded as [HomeUiState.googleError] and clears the
     * summary; it is never drawn as "not connected", which is a different fact
     * and would send the person to fix the wrong thing. The label comes from
     * the model's own stateLabel so this screen cannot invent a state the rest
     * of the app does not recognise.
     */
    private suspend fun loadGoogle() {
        try {
            val status = api.fetchGoogleStatus()
            _state.value = _state.value.copy(
                googleSummary = status.connection.stateLabel,
                googleAccount = status.connection.account.orEmpty(),
                googleConnected = status.connection.isConnected,
                googleError = null,
            )
        } catch (e: Exception) {
            _state.value = _state.value.copy(
                googleSummary = null,
                googleAccount = "",
                googleConnected = false,
                googleError = when (e) {
                    is ApiException.Transport -> "Could not reach the laptop"
                    is ApiException.Unauthorized -> "Session expired — go to Settings to reconnect"
                    else -> e.message ?: "Could not read the Google connection"
                },
            )
        }
    }

    class Factory(
        private val api: VantageApiClient,
        private val store: SessionStore,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T =
            HomeViewModel(api, store) as T
    }
}