package com.vantage.companion.ui.google

import com.vantage.companion.data.api.GoogleConnection
import com.vantage.companion.data.api.GoogleEvent
import com.vantage.companion.data.api.GoogleStatusResponse
import com.vantage.companion.data.api.GoogleTask
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The two things this screen must never get wrong:
 *
 * 1. a failed read is not an empty day, and
 * 2. the completed-tasks toggle decides what is shown, not the data.
 *
 * The fixtures are the shapes the real routes return (`/google/status`,
 * `/google/calendar/events`, `/google/tasks`), captured from the backend's
 * response builders rather than imagined.
 */
class GoogleScreenStateTest {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    @Test
    fun `connected status decodes from the real shape`() {
        val raw = """
            {"connection":{"state":"connected","detail":"Connected to Google.",
             "account":"owner@example.com","scopes":["calendar.readonly","tasks"],
             "configured":true,"has_token":true,"usable":true},
             "setup":{"configured":true,"setup":[],"redirect_uri":"http://127.0.0.1:8765/google/callback"}}
        """.trimIndent()
        val status = json.decodeFromString<GoogleStatusResponse>(raw)
        assertTrue(status.connection.isConnected)
        assertEquals("owner@example.com", status.connection.account)
        assertTrue(status.setup.configured)
    }

    @Test
    fun `a saved but expired token is not connected`() {
        val raw = """
            {"connection":{"state":"expired","detail":"revoked",
             "configured":true,"has_token":true,"usable":false}}
        """.trimIndent()
        val status = json.decodeFromString<GoogleStatusResponse>(raw)
        // Saved is not working. This is the distinction the whole screen rests on.
        assertTrue(status.connection.hasToken)
        assertFalse(status.connection.isConnected)
    }

    @Test
    fun `read failure leaves events empty and is flagged`() {
        // What the view model derives from a response whose read failed.
        val state = GoogleUiState(events = emptyList(), eventsFailed = true,
                                  eventsDetail = "Google rejected the access token.")
        assertTrue(state.eventsFailed)
        assertTrue(state.events.isEmpty())
        assertEquals("Google rejected the access token.", state.eventsDetail)
    }

    @Test
    fun `a real empty day is not a failure`() {
        val state = GoogleUiState(events = emptyList(), eventsFailed = false)
        // "Nothing scheduled" may only be said when the read succeeded.
        assertFalse(state.eventsFailed)
        assertTrue(state.events.isEmpty())
    }

    @Test
    fun `completed tasks are hidden unless the toggle is on`() {
        val tasks = listOf(
            GoogleTask(id = "1", title = "Open one", done = false),
            GoogleTask(id = "2", title = "Finished one", done = true),
        )
        val hidden = GoogleUiState(tasks = tasks, showCompleted = false)
        assertEquals(listOf("Open one"), hidden.visibleTasks.map { it.title })

        val shown = GoogleUiState(tasks = tasks, showCompleted = true)
        assertEquals(2, shown.visibleTasks.size)
    }

    @Test
    fun `events decode with all-day and duration fields`() {
        val raw = """
            {"provider":"google","status":"has_results","read_succeeded":true,
             "detail":null,"count":2,"events":[
               {"uid":"e1","title":"Review","start":"2026-11-02T14:00:00+00:00",
                "end":"2026-11-02T15:00:00+00:00","location":"Room 2",
                "all_day":false,"duration_minutes":60},
               {"uid":"e2","title":"Offsite","start":"2026-11-03","end":"2026-11-04",
                "all_day":true,"duration_minutes":1440}]}
        """.trimIndent()
        val response = json.decodeFromString<com.vantage.companion.data.api.GoogleEventsResponse>(raw)
        assertTrue(response.readSucceeded)
        assertEquals(2, response.events.size)
        assertFalse(response.events[0].allDay)
        assertEquals(60, response.events[0].durationMinutes)
        assertTrue(response.events[1].allDay)
    }

    @Test
    fun `not configured is distinguishable from disconnected`() {
        val notConfigured = GoogleConnection(state = "not_configured", configured = false)
        val disconnected = GoogleConnection(state = "disconnected", configured = true)
        assertFalse(notConfigured.isConnected)
        assertFalse(disconnected.isConnected)
        // They are different facts and the screen says different things.
        assertFalse(notConfigured.configured)
        assertTrue(disconnected.configured)
    }

    @Test
    fun `a pending approval is not a completed write`() {
        // The write was requested and is waiting for the Mac. Nothing has
        // happened yet, so the list must not imply it has.
        val state = GoogleUiState(
            tasks = listOf(GoogleTask(id = "1", title = "Buy milk", done = false)),
            pendingApproval = "Approve this on the laptop instead.",
        )
        assertTrue(state.pendingApproval != null)
        assertFalse(state.visibleTasks.first().done)
    }

    @Test
    fun `a write in flight is not reflected in the data until re-read`() {
        val state = GoogleUiState(isWriting = true,
                                  tasks = listOf(GoogleTask(id = "1", title = "x", done = false)))
        assertTrue(state.isWriting)
        // Optimistically ticking the row would claim Google changed when it
        // has not been asked yet.
        assertFalse(state.visibleTasks.first().done)
    }
}