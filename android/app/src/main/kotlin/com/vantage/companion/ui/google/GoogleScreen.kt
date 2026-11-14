package com.vantage.companion.ui.google

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import com.vantage.companion.data.api.ConnectionTone
import com.vantage.companion.data.api.GoogleEvent
import com.vantage.companion.data.api.GoogleTask
import com.vantage.companion.ui.components.*
import com.vantage.companion.ui.theme.*

/**
 * Google Calendar and Tasks on the phone.
 *
 * Read-only by design: the connection is authorised on the Mac and this screen
 * shows what it holds. A failed read is drawn as a failure with the reason --
 * never as an empty day -- because "nothing scheduled" and "could not ask" are
 * different facts and only one of them means the person is free.
 *
 * Writes (add / complete) are requested through /command and approved on the
 * Mac. The phone cannot approve its own request, so the screen re-reads
 * whenever it is shown and offers pull-to-refresh: that is how an approval
 * made elsewhere becomes visible here.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GoogleScreen(viewModel: GoogleViewModel) {
    val state by viewModel.state.collectAsState()

    // Re-enter the tab, or return to the app, after the Mac approved: read
    // again so the change appears without the person having to ask. ON_START
    // fires when this screen becomes current and on app foreground; ON_RESUME
    // covers a return from another activity. Both route through
    // onScreenShown(), which re-reads quietly once a status is known.
    LifecycleEventEffect(Lifecycle.Event.ON_START) {
        viewModel.onScreenShown()
    }
    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) {
        viewModel.onScreenShown()
    }

    Column(modifier = Modifier.fillMaxSize()) {
        // The top banner is for errors while a status is known; an unknown
        // status is drawn below in full, with its own retry, so the screen
        // never shows two retry controls for the same failure.
        if (state.error != null && !state.statusUnknown) {
            Notice(
                kind = NoticeKind.Error,
                message = state.error!!,
                modifier = Modifier.padding(horizontal = Space.screen, vertical = Space.s),
                actionTitle = "Retry",
                onAction = { viewModel.refresh() },
            )
        }

        if (state.isLoading && state.status == null) {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                LoadingRow("Checking the Google connection…")
            }
            return
        }

        // A status check that failed (status == null, not loading) is drawn as
        // a retryable failure -- never an endless spinner or a silent
        // "Checking…" that never resolves. The retry sits inside the view so
        // it stays reachable without the top error banner.
        if (state.statusUnknown) {
            Column(
                modifier = Modifier.fillMaxSize().padding(Space.screen),
                verticalArrangement = Arrangement.spacedBy(Space.s),
            ) {
                ReadFailure(
                    state.error
                        ?: "Could not check the Google connection.",
                )
                TextButton(onClick = { viewModel.refresh() }) {
                    Text("Retry", color = Accent, fontSize = 12.sp)
                }
            }
            return
        }

        PullToRefreshBox(
            isRefreshing = state.isLoading,
            onRefresh = { viewModel.refresh() },
            modifier = Modifier.fillMaxSize(),
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .verticalScroll(rememberScrollState())
                    .padding(Space.screen),
                verticalArrangement = Arrangement.spacedBy(Space.l),
            ) {
                ConnectionCard(state, onRefresh = { viewModel.refresh() })

                if (state.connected) {
                    Column(verticalArrangement = Arrangement.spacedBy(Space.s)) {
                        SectionHeader("Today")
                        if (state.eventsFailed) {
                            ReadFailure(state.eventsDetail ?: "Google Calendar could not be read.")
                        } else if (state.events.isEmpty()) {
                            Text("Nothing scheduled today.", color = Muted, fontSize = 13.sp)
                        } else {
                            state.events.forEach { EventRow(it) }
                        }
                    }

                    Column(verticalArrangement = Arrangement.spacedBy(Space.s)) {
                        SectionHeader(
                            title = "Tasks",
                            trailing = {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text("Completed", color = Muted, fontSize = 11.sp)
                                    Spacer(Modifier.width(Space.xs))
                                    Switch(
                                        checked = state.showCompleted,
                                        onCheckedChange = { viewModel.setShowCompleted(it) },
                                        modifier = Modifier.height(24.dp),
                                    )
                                }
                            },
                        )
                        if (state.tasksFailed) {
                            ReadFailure(state.tasksDetail ?: "Google Tasks could not be read.")
                        } else if (state.visibleTasks.isEmpty()) {
                            Text(
                                if (state.showCompleted) "No tasks in this list."
                                else "No open tasks.",
                                color = Muted, fontSize = 13.sp,
                            )
                        } else {
                            state.visibleTasks.forEach { task ->
                                TaskRow(
                                    task = task,
                                    // A write in flight locks the list too, so two
                                    // requests cannot race for one row's approval.
                                    enabled = !state.isWriting,
                                    onComplete = { viewModel.completeTask(it) },
                                )
                            }
                        }

                        AddTaskRow(enabled = !state.isWriting) { title ->
                            viewModel.addTask(title)
                        }
                    }

                    state.pendingApproval?.let { PendingApproval(it) }

                    Text(
                        "Events and tasks are read from Google directly and are not " +
                            "copied into Vantage, so nothing becomes two reminders. " +
                            "Adding or completing a task is approved on the Mac; " +
                            "pull to refresh once it is.",
                        color = Faint, fontSize = 11.sp,
                    )
                }
            }
        }
    }
}

@Composable
private fun ConnectionCard(state: GoogleUiState, onRefresh: () -> Unit) {
    val connection = state.status?.connection
    // The tone and the label live on the model so they are unit-tested and
    // cannot disagree. An unknown state is neutral and named as unknown --
    // never blank, never "connected".
    val kind = when (connection?.tone) {
        ConnectionTone.CONNECTED -> StatusKind.Connected
        ConnectionTone.ERROR -> StatusKind.Error
        ConnectionTone.UNKNOWN -> StatusKind.Pending
        null, ConnectionTone.NEUTRAL -> StatusKind.Idle
    }
    val title = connection?.stateLabel ?: "Checking…"

    VCard {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(Space.s),
        ) {
            Text(
                "Google connection",
                color = Ink,
                fontSize = 14.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.weight(1f),
            )
            StatusPill(kind, title)
        }
        TextButton(onClick = onRefresh, modifier = Modifier.align(Alignment.End)) {
            Text("Refresh", color = Accent, fontSize = 11.sp)
        }
        connection?.detail?.takeIf { it.isNotBlank() }?.let {
            Spacer(Modifier.height(Space.xs))
            Text(it, color = Muted, fontSize = 12.sp)
        }
        connection?.account?.takeIf { it.isNotBlank() }?.let {
            Spacer(Modifier.height(2.dp))
            Text(it, color = Muted, fontSize = 11.sp)
        }

        // What is missing, when nothing is set up. Read-only on the phone:
        // the authorisation happens on the Mac, so this says where.
        if (!state.configured) {
            val steps = state.status?.setup?.setup.orEmpty()
            if (steps.isNotEmpty()) {
                Spacer(Modifier.height(Space.s))
                Text("Connect Google on the Mac that runs Vantage.",
                    color = Ink, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                steps.forEachIndexed { index, step ->
                    Text("${index + 1}. $step", color = Muted, fontSize = 11.sp)
                }
            }
        }
    }
}

@Composable
private fun EventRow(event: GoogleEvent) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Text(
            if (event.allDay) "all day" else clockTime(event.start),
            color = Muted, fontSize = 12.sp,
            modifier = Modifier.width(64.dp),
        )
        Column(Modifier.weight(1f)) {
            Text(event.title, color = Ink, fontSize = 13.sp)
            event.location?.takeIf { it.isNotBlank() }?.let {
                Text(it, color = Muted, fontSize = 11.sp)
            }
        }
    }
}

@Composable
private fun TaskRow(
    task: GoogleTask,
    enabled: Boolean,
    onComplete: (GoogleTask) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(Space.s),
        verticalAlignment = Alignment.Top,
    ) {
        // Tapping the circle asks to complete the task. The backend treats it
        // as an EXTERNAL write and asks the Mac for approval, so the row does
        // not optimistically tick itself: the next read is the truth. A done
        // task, an untitled one (which cannot be named in a command), or a
        // write already in flight cannot be asked again.
        IconButton(
            onClick = { onComplete(task) },
            enabled = enabled && task.isCompletable,
            modifier = Modifier.size(24.dp),
        ) {
            Icon(
                if (task.done) Icons.Filled.CheckCircle else Icons.Filled.RadioButtonUnchecked,
                contentDescription = if (task.done) "Completed" else "Mark completed",
                tint = if (task.done) Accent else Muted,
                modifier = Modifier.size(16.dp),
            )
        }
        Column(Modifier.weight(1f)) {
            Text(
                task.displayTitle,
                color = if (task.done) Muted else Ink,
                fontSize = 13.sp,
            )
            task.notes?.takeIf { it.isNotBlank() }?.let {
                Text(it, color = Muted, fontSize = 11.sp, maxLines = 2)
            }
            task.due?.let {
                // Google Tasks stores a date, not a time.
                Text("due $it", color = Muted, fontSize = 11.sp)
            }
        }
    }
}

/** Add a task. The write is requested here and approved on the Mac. */
@Composable
private fun AddTaskRow(enabled: Boolean, onAdd: (String) -> Unit) {
    var title by remember { mutableStateOf("") }
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(Space.s),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        OutlinedTextField(
            value = title,
            onValueChange = { title = it },
            placeholder = { Text("Add a Google task", fontSize = 12.sp, color = Faint) },
            singleLine = true,
            enabled = enabled,
            shape = RoundedCornerShape(Radius.control),
            modifier = Modifier.weight(1f),
            colors = OutlinedTextFieldDefaults.colors(
                focusedBorderColor = Accent,
                unfocusedBorderColor = FieldLine,
                disabledBorderColor = Line,
            ),
        )
        TextButton(
            onClick = {
                val clean = title.trim()
                if (clean.isNotEmpty()) {
                    onAdd(clean)
                    title = ""
                }
            },
            enabled = enabled && title.isNotBlank(),
        ) {
            Text("Add", color = Accent, fontSize = 12.sp)
        }
    }
}

/** Say plainly that nothing was written yet, and who can approve it. */
@Composable
private fun PendingApproval(reply: String) {
    Notice(kind = NoticeKind.Info, message = reply)
}

/** One place that draws a failed read, so no section can soften it. */
@Composable
private fun ReadFailure(detail: String) {
    Notice(kind = NoticeKind.Warning, message = detail)
}

/** RFC3339 -> "HH:mm" in the device's own timezone; falls back to the raw value. */
private fun clockTime(iso: String?): String {
    if (iso.isNullOrBlank()) return "—"
    return try {
        val instant = java.time.OffsetDateTime.parse(iso)
        instant.atZoneSameInstant(java.time.ZoneId.systemDefault())
            .format(java.time.format.DateTimeFormatter.ofPattern("HH:mm"))
    } catch (e: Exception) {
        iso
    }
}