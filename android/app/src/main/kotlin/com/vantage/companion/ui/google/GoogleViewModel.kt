package com.vantage.companion.ui.google

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.vantage.companion.data.api.*
import com.vantage.companion.data.local.SessionStore
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * What the Google screen is showing, and why.
 *
 * `eventsFailed` / `tasksFailed` are separate from an empty list on purpose: a
 * read that failed and a day with nothing on it look identical if the
 * distinction is thrown away, and telling someone they are free when the
 * calendar could not be read is the one mistake this screen must not make.
 *
 * The phone can read Google, never connect or disconnect it. That is enforced
 * server-side (the connection routes are same-origin protected and are absent
 * from the gateway's allowlist); this view model simply has no method for it.
 *
 * A task may be added or completed from here, but only through `/command`,
 * which is the surface that carries the confirmation workflow. Both are
 * EXTERNAL writes, so the backend raises a confirmation instead of acting;
 * because the request is raised by a *phone* session, the self-approval guard
 * means this handset cannot approve it either. `pendingApproval` is how that is
 * said plainly: the request exists, and the Mac has to answer it.
 */
data class GoogleUiState(
    val isLoading: Boolean = true,
    val status: GoogleStatusResponse? = null,
    val events: List<GoogleEvent> = emptyList(),
    val eventsFailed: Boolean = false,
    val eventsDetail: String? = null,
    val tasks: List<GoogleTask> = emptyList(),
    val tasksFailed: Boolean = false,
    val tasksDetail: String? = null,
    val showCompleted: Boolean = false,
    val error: String? = null,
    /** True while a task write is in flight. */
    val isWriting: Boolean = false,
    /** Set when the backend asked for approval instead of acting. */
    val pendingApproval: String? = null,
) {
    val connected: Boolean get() = status?.connection?.isConnected == true
    val configured: Boolean get() = status?.setup?.configured == true
    val visibleTasks: List<GoogleTask>
        get() = if (showCompleted) tasks else tasks.filter { !it.done }

    /**
     * A status check that failed leaves [status] null with an [error]. The
     * connection card must say so instead of sitting on "Checking…" forever:
     * an unanswered question is not a pending one.
     */
    val statusUnknown: Boolean get() = status == null && !isLoading
}

class GoogleViewModel(
    private val api: VantageApiClient,
    private val store: SessionStore,
) : ViewModel() {

    private val _state = MutableStateFlow(GoogleUiState())
    val state: StateFlow<GoogleUiState> = _state.asStateFlow()

    private var refreshJob: Job? = null
    private var tasksJob: Job? = null
    // Bumped by every full refresh. A superseded refresh must not clear
    // isLoading after its replacement has set it.
    private var refreshSeq = 0

    init {
        refresh()
    }

    /**
     * Re-read the connection, the day and the tasks.
     *
     * This is also the route back after a write was approved on the Mac: the
     * phone cannot approve its own request, so the only way the list can learn
     * that it was answered is to read again. The screen calls [onScreenShown]
     * whenever it is reopened, and the pull-to-refresh gesture calls this.
     * A second refresh while one is running cancels the first rather than
     * stacking requests, so the loading state can always end.
     */
    fun refresh() {
        refreshJob?.cancel()
        val seq = ++refreshSeq
        refreshJob = viewModelScope.launch(Dispatchers.IO) {
            _state.value = _state.value.copy(isLoading = true)
            try {
                val status = api.fetchGoogleStatus()
                _state.value = _state.value.copy(status = status, error = null)
                if (status.connection.isConnected) {
                    // Reads run inside this job (not as detached launches) so
                    // isLoading covers them and cancellation is coherent.
                    readEvents()
                    readTasks()
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    error = readableMessage(e, "Could not reach the backend"),
                )
            } finally {
                if (seq == refreshSeq) {
                    _state.value = _state.value.copy(isLoading = false)
                }
            }
        }
    }

    /**
     * Called every time the screen becomes current (ON_START/ON_RESUME). The
     * first call is the ViewModel init; later calls (returning from Approvals,
     * switching tabs after approving on the Mac) re-read quietly so an
     * approved task shows up without the full-screen spinner.
     */
    fun onScreenShown() {
        if (_state.value.status == null) {
            refresh()
        } else {
            refreshQuietly()
        }
    }

    /** Re-read events and tasks in place, leaving the status card as it is. */
    fun refreshQuietly() {
        if (refreshJob?.isActive == true) return
        refreshJob = viewModelScope.launch(Dispatchers.IO) {
            try {
                val status = api.fetchGoogleStatus()
                _state.value = _state.value.copy(status = status, error = null)
                if (status.connection.isConnected) {
                    readEvents()
                    readTasks()
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    error = readableMessage(e, "Could not reach the backend"),
                )
            }
        }
    }

    fun setShowCompleted(show: Boolean) {
        _state.value = _state.value.copy(showCompleted = show)
        loadTasks()
    }

    /**
     * Ask the backend to add a Google task.
     *
     * The sentence names Google explicitly, which is what routes it to
     * `add_google_task` rather than the local task list. The write is
     * EXTERNAL, so a successful response is normally a *confirmation request*,
     * not a created task -- that is why `pendingApproval` is reported rather
     * than refreshing the list as if something had happened. Only when the
     * backend says it acted outright is the list re-read.
     */
    fun addTask(title: String, due: String = "") {
        val clean = title.trim()
        if (clean.isBlank() || _state.value.isWriting) return
        val text = buildString {
            append("add a task to Google Tasks ")
            append(clean)
            if (due.isNotBlank()) append(" by ").append(due.trim())
        }
        sendWrite(text)
    }

    /**
     * Ask the backend to complete a Google task by its title. A done task is
     * not askable again, and a task with no title cannot be named.
     */
    fun completeTask(task: GoogleTask) {
        if (!task.isCompletable || _state.value.isWriting) return
        sendWrite("mark ${task.title.trim()} done in Google Tasks")
    }

    private fun sendWrite(text: String) {
        _state.value = _state.value.copy(isWriting = true, error = null, pendingApproval = null)
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val response = api.sendCommand(
                    CommandRequest(
                        text = text,
                        deviceId = store.deviceId.value.ifBlank { "phone" },
                        source = "mobile",
                        sessionId = store.sessionToken(),
                    ),
                )
                val acted = !response.confirmationRequired && !response.isBlocked &&
                    response.actions.any { it.ok == true }
                _state.value = _state.value.copy(
                    pendingApproval = if (response.confirmationRequired) {
                        response.reply
                            ?: "Vantage asked for approval before writing to Google. " +
                                "Open Approvals to review and confirm it."
                    } else null,
                    error = if (response.isBlocked) {
                        response.blockedReason ?: response.reply
                    } else null,
                )
                // The backend acted without asking (e.g. a low-risk path):
                // the next read is the truth about what Google now holds.
                if (acted) {
                    readTasks()
                    readEvents()
                }
            } catch (e: ApiException.Unauthorized) {
                _state.value = _state.value.copy(
                    error = "Session expired. Go to Settings to reconnect.")
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    error = readableMessage(e, "Could not reach the laptop."))
            } finally {
                _state.value = _state.value.copy(isWriting = false)
            }
        }
    }

    private suspend fun readEvents() {
        try {
            val response = api.fetchGoogleEvents()
            _state.value = _state.value.copy(
                events = if (response.readSucceeded) response.events else emptyList(),
                // A failure is recorded as a failure, not as an empty day.
                eventsFailed = !response.readSucceeded,
                eventsDetail = response.detail,
            )
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _state.value = _state.value.copy(
                eventsFailed = true,
                eventsDetail = readableMessage(e, "Could not read the calendar"),
            )
        }
    }

    private suspend fun readTasks() {
        try {
            val response = api.fetchGoogleTasks(
                showCompleted = _state.value.showCompleted,
            )
            _state.value = _state.value.copy(
                tasks = if (response.readSucceeded) response.tasks else emptyList(),
                tasksFailed = !response.readSucceeded,
                tasksDetail = response.detail,
            )
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _state.value = _state.value.copy(
                tasksFailed = true,
                tasksDetail = readableMessage(e, "Could not read tasks"),
            )
        }
    }

    /** A toggle must not cancel an in-flight full refresh or stack on itself. */
    private fun loadTasks() {
        if (!_state.value.connected) return
        tasksJob?.cancel()
        tasksJob = viewModelScope.launch(Dispatchers.IO) { readTasks() }
    }

    /** Transport text is diagnostic; the screen shows a sentence a person can act on. */
    private fun readableMessage(e: Exception, fallback: String): String = when (e) {
        is ApiException.Transport -> "Could not reach the laptop. Check the connection and retry."
        is ApiException.Unauthorized -> "Session expired. Go to Settings to reconnect."
        is ApiException.TokenRevoked -> "This phone was signed out. Go to Settings to reconnect."
        is PinnedCertificateTrustManager.MismatchException ->
            "The laptop's certificate does not match. Re-pair from Settings."
        else -> e.message ?: fallback
    }

    class Factory(
        private val api: VantageApiClient,
        private val store: SessionStore,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T {
            return GoogleViewModel(api, store) as T
        }
    }
}