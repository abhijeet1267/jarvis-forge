package com.vantage.companion.ui.command

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
import java.util.UUID

data class CommandEntry(
    val id: String = UUID.randomUUID().toString(),
    val text: String,
    val role: CommandRole,
    val status: String? = null,
    val verified: Boolean = false,
    val confirmationRequired: Boolean = false,
    val taskId: String? = null,
)

enum class CommandRole { USER, ASSISTANT, ERROR }

class CommandViewModel(
    private val api: VantageApiClient,
    private val store: SessionStore,
    private val voice: VoiceController? = null,
) : ViewModel() {

    private val _history = MutableStateFlow<List<CommandEntry>>(emptyList())
    val history: StateFlow<List<CommandEntry>> = _history.asStateFlow()

    // ── AI Voice (same persisted preference as Settings) ──
    val voiceEnabled: StateFlow<Boolean> =
        voice?.enabled ?: MutableStateFlow(false)
    val voiceAvailability: StateFlow<VoiceAvailability> =
        voice?.availability ?: MutableStateFlow(VoiceAvailability.NOT_INITIALIZED)

    /** Header control: toggle the one shared preference. */
    fun toggleVoice() {
        voice?.toggle()
    }

    private val _isSending = MutableStateFlow(false)
    val isSending: StateFlow<Boolean> = _isSending.asStateFlow()

    private val _input = MutableStateFlow("")
    val input: StateFlow<String> = _input.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    val quickChips = listOf(
        "What can you help me with?",
        "Show my pending approvals",
        "What's my system status?",
        "Summarize my recent activity",
        "List available tools",
    )

    fun updateInput(value: String) {
        _input.value = value
    }

    fun send() {
        val text = _input.value.trim()
        if (text.isBlank() || _isSending.value) return
        deliver(text)
    }

    /**
     * The body of a send, without the composer's early-return.
     *
     * Split out so [retry] can re-send through exactly the same path. That split
     * found a real defect: retry used to call [send], which re-appended the
     * question after retry had dropped the failed turn — so one retry produced
     * two copies of the question in the transcript, and a test that only counted
     * requests would not have seen it.
     */
    private fun deliver(text: String) {
        val deviceId = store.deviceId.value.ifBlank { "phone" }

        _history.value = _history.value + CommandEntry(
            text = text,
            role = CommandRole.USER,
        )
        _input.value = ""
        _error.value = null
        _isSending.value = true

        viewModelScope.launch(Dispatchers.IO) {
            try {
                val cmd = CommandRequest(
                    text = text,
                    deviceId = deviceId,
                    source = "mobile",
                )
                val response = api.sendCommand(cmd)

                val entry = CommandEntry(
                    text = response.displayText,
                    role = when {
                        response.isBlocked -> CommandRole.ERROR
                        else -> CommandRole.ASSISTANT
                    },
                    status = response.decision,
                    verified = response.hasVerifiedEffect,
                    confirmationRequired = response.confirmationRequired,
                    taskId = response.requestId,
                )
                _history.value = _history.value + entry
                // Speak only a real assistant answer. The controller's policy
                // rejects OFF, unavailable, blocked and confirmation-required
                // replies, and dedupes on request id so a retry of the same
                // reply is not spoken twice.
                voice?.announce(response)
            } catch (e: ApiException.Unauthorized) {
                _history.value = _history.value + CommandEntry(
                    text = "Session expired. Go to Settings to reconnect.",
                    role = CommandRole.ERROR,
                )
                _error.value = "Session expired"
            } catch (e: ApiException.Transport) {
                _history.value = _history.value + CommandEntry(
                    text = "Could not reach the laptop. Check your connection.",
                    role = CommandRole.ERROR,
                )
                _error.value = "Network error"
            } catch (e: Exception) {
                _history.value = _history.value + CommandEntry(
                    text = e.message ?: "Command failed",
                    role = CommandRole.ERROR,
                )
                _error.value = e.message
            } finally {
                _isSending.value = false
            }
        }
    }

    /**
     * Re-send the last thing the person asked, through the same guarded [send].
     *
     * This lived only in the screen, which meant the recovery path had no test:
     * a retry could duplicate the question and nothing would catch it. It is a
     * ViewModel method so the guard is exercised directly. Anything typed since
     * is put back in the composer afterwards, so a retry never discards a draft.
     */
    fun retry() {
        if (_isSending.value) return
        val history = _history.value
        val lastIndex = history.indexOfLast { it.role == CommandRole.USER }
        if (lastIndex < 0) return
        val last = history[lastIndex]
        // Only the *last* turn may be dropped. If anything arrived after the
        // question (an answer, an error, another exchange) the question is
        // already part of the record and must stay.
        val isLastTurn = lastIndex == history.lastIndex
        if (isLastTurn) _history.value = history.dropLast(1)

        // Re-send through the same guarded path, without the composer trip.
        // The draft must be captured *before* deliver() clears the composer --
        // reading it afterwards always yields "" and silently loses what the
        // person had started typing.
        val draft = _input.value
        _input.value = last.text
        deliver(last.text)
        if (draft.isNotBlank()) _input.value = draft
    }

    fun clearError() {
        _error.value = null
    }

    fun clearHistory() {
        _history.value = emptyList()
    }

    class Factory(
        private val api: VantageApiClient,
        private val store: SessionStore,
        private val voice: VoiceController? = null,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T =
            CommandViewModel(api, store, voice) as T
    }
}