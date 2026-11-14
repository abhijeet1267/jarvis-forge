package com.vantage.companion.ui.google

import com.vantage.companion.data.api.*
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Test

/**
 * The Android models must decode the bytes the backend actually sends.
 *
 * The fixtures are captured from a live isolated server, not imagined — the
 * project has already been bitten once by a Kotlin model that tested fields
 * the backend does not return (see ApiModelsTest). If the backend ever renames
 * a field, this fails rather than silently showing an empty screen.
 */
class RealShapeDecodeTest {

    // The decoder production uses (WireJson adds coerceInputValues, which is
    // what makes an explicit null for a non-nullable field fall back to its
    // default instead of throwing).
    private val json = WireJson
    // Captured from a live isolated server (/google/status, /google/calendar/events,
    // /google/tasks) and pasted here so the test needs no external file.
    private val statusJson = """{
    "connection": {
        "state": "not_configured",
        "detail": "Google is not configured. Set JARVIS_GOOGLE_CLIENT_ID.",
        "account": "",
        "scopes": [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/tasks"
        ],
        "configured": false,
        "has_token": false,
        "usable": false
    },
    "setup": {
        "configured": false,
        "calendar_enabled": true,
        "tasks_enabled": true,
        "scopes": [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/tasks"
        ],
        "setup": [
            "Create a project at console.cloud.google.com",
            "Enable the Google Calendar API and the Google Tasks API",
            "Create an OAuth client of type 'Desktop app'",
            "Set JARVIS_GOOGLE_CLIENT_ID to its client id"
        ],
        "redirect_uri": "http://127.0.0.1:8765/google/callback"
    }
}"""
    private val eventsJson = """{
    "provider": "google",
    "status": "not_configured",
    "read_succeeded": false,
    "detail": "Google is not configured (JARVIS_GOOGLE_CLIENT_ID is empty).",
    "count": 0,
    "events": []
}"""
    private val tasksJson = """{
    "provider": "google_tasks",
    "status": "not_configured",
    "read_succeeded": false,
    "detail": "Google is not configured (JARVIS_GOOGLE_CLIENT_ID is empty).",
    "count": 0,
    "tasks": []
}"""

    @Test
    fun `status decodes from the real payload`() {
        val status = json.decodeFromString<GoogleStatusResponse>(statusJson)
        assertEquals("not_configured", status.connection.state)
        assertFalse(status.connection.isConnected)
        assertFalse(status.setup.configured)
        // The setup steps are what the screen renders when unconfigured.
        assertTrue(status.setup.setup.isNotEmpty())
    }

    @Test
    fun `events decodes and a failed read is visible as failed`() {
        val events = json.decodeFromString<GoogleEventsResponse>(eventsJson)
        assertFalse(events.readSucceeded)
        assertEquals("not_configured", events.status)
        assertTrue(events.events.isEmpty())
        assertNotNull(events.detail)
    }

    @Test
    fun `tasks decodes and a failed read is visible as failed`() {
        val tasks = json.decodeFromString<GoogleTasksResponse>(tasksJson)
        assertFalse(tasks.readSucceeded)
        assertTrue(tasks.tasks.isEmpty())
    }

    @Test
    fun `an unknown status does not throw`() {
        // A newer backend may add a state this app has never heard of.
        val raw = """{"connection":{"state":"something_new","usable":false},"setup":{}}"""
        val status = json.decodeFromString<GoogleStatusResponse>(raw)
        assertEquals("something_new", status.connection.state)
        assertFalse(status.connection.isConnected)
    }

    @Test
    fun `a missing connection object does not throw`() {
        val status = json.decodeFromString<GoogleStatusResponse>("{}")
        assertFalse(status.connection.isConnected)
    }

    // ------------------------------------------------------------------
    // Bad data. The models are decoded with the production decoder
    // (VantageApiClient's WireJson via coerceInputValues), so these are the
    // guarantees the app actually has -- not a stricter test-only Json.
    // ------------------------------------------------------------------

    @Test
    fun `production decoder is the one under test`() {
        // The tests must exercise the decoder the app ships with, otherwise a
        // lenient test Json could bless shapes production still throws on.
        val status = json.decodeFromString<GoogleStatusResponse>(
            """{"connection":{"state":"connected","usable":true}}"""
        )
        assertTrue(status.connection.isConnected)
        assertTrue(json.configuration.coerceInputValues)
        assertTrue(json.configuration.ignoreUnknownKeys)
    }

    @Test
    fun `a null connection object falls back to the default`() {
        // A degraded backend may serialise the object as null. coerceInputValues
        // is what turns that into "unknown connection" rather than a throw.
        val status = json.decodeFromString<GoogleStatusResponse>("""{"connection":null,"setup":null}""")
        assertFalse(status.connection.isConnected)
        assertEquals("", status.connection.state)
        assertEquals("Google status unavailable", status.connection.stateLabel)
    }

    @Test
    fun `a connection with no state string does not crash and is not connected`() {
        val status = json.decodeFromString<GoogleStatusResponse>("""{"connection":{"usable":false}}""")
        assertFalse(status.connection.isConnected)
        assertEquals("Google status unavailable", status.connection.stateLabel)
        assertEquals(ConnectionTone.UNKNOWN, status.connection.tone)
    }

    @Test
    fun `an unknown state is neutral, not blank and not connected`() {
        val connection = json.decodeFromString<GoogleConnection>(
            """{"state":"needs_reauth_v2","usable":false}"""
        )
        assertEquals("Google status: needs_reauth_v2", connection.stateLabel)
        assertEquals(ConnectionTone.UNKNOWN, connection.tone)
        assertFalse(connection.isConnected)
    }

    @Test
    fun `connected state without a passing live check is not claimed as connected`() {
        // The backend only says "connected" after a live call succeeded, but a
        // stale or partial payload could pair it with usable=false. Honesty
        // wins: the label says the connection is unconfirmed.
        val connection = GoogleConnection(state = "connected", usable = false)
        assertFalse(connection.isConnected)
        assertEquals("Connection not confirmed", connection.stateLabel)
        assertEquals(ConnectionTone.UNKNOWN, connection.tone)
    }

    @Test
    fun `every known backend state maps to its own truthful label`() {
        // ConnectionState in jarvis/integrations/google_auth.py.
        assertEquals(ConnectionTone.NEUTRAL, GoogleConnection(state = "not_configured").tone)
        assertEquals(ConnectionTone.NEUTRAL, GoogleConnection(state = "disconnected").tone)
        assertEquals(ConnectionTone.ERROR, GoogleConnection(state = "expired").tone)
        assertEquals(ConnectionTone.ERROR, GoogleConnection(state = "error").tone)
        assertEquals(ConnectionTone.CONNECTED,
            GoogleConnection(state = "connected", usable = true).tone)
        // No known label is ever blank.
        for (state in listOf("not_configured", "disconnected", "connected",
                             "expired", "error", "", "something_new")) {
            assertTrue(GoogleConnection(state = state).stateLabel.isNotBlank())
        }
    }

    @Test
    fun `a null tasks list decodes as no tasks, not a crash`() {
        val tasks = json.decodeFromString<GoogleTasksResponse>(
            """{"read_succeeded":true,"count":0,"tasks":null}"""
        )
        assertTrue(tasks.readSucceeded)
        assertTrue(tasks.tasks.isEmpty())
    }

    @Test
    fun `a null events list decodes as no events, not a crash`() {
        val events = json.decodeFromString<GoogleEventsResponse>(
            """{"read_succeeded":true,"events":null}"""
        )
        assertTrue(events.events.isEmpty())
    }

    @Test
    fun `a task with blank id and title decodes and is not completable`() {
        val response = json.decodeFromString<GoogleTasksResponse>(
            """{"read_succeeded":true,"count":1,"tasks":[
                 {"id":"","title":"","status":"needsAction","done":false}]}"""
        )
        val task = response.tasks.single()
        assertEquals("", task.id)
        assertEquals("(untitled task)", task.displayTitle)
        assertFalse(task.isCompletable)
    }

    @Test
    fun `a task with a whitespace-only title is treated as untitled`() {
        val task = GoogleTask(id = "t1", title = "   ")
        assertEquals("(untitled task)", task.displayTitle)
        assertFalse(task.isCompletable)
    }

    @Test
    fun `a done task is not completable`() {
        val task = GoogleTask(id = "t1", title = "Done already", done = true)
        assertFalse(task.isCompletable)
    }

    @Test
    fun `an open titled task is completable`() {
        assertTrue(GoogleTask(id = "t1", title = "Buy milk").isCompletable)
    }

    @Test
    fun `unexpected extra fields are ignored`() {
        val status = json.decodeFromString<GoogleStatusResponse>(
            """{"connection":{"state":"connected","usable":true,"future_field":{"a":1}},
                "setup":{},"brand_new_top_level":[1,2,3]}"""
        )
        assertTrue(status.connection.isConnected)
        val tasks = json.decodeFromString<GoogleTasksResponse>(
            """{"read_succeeded":true,"tasks":[{"id":"1","title":"x","done":false,
                "new_backend_field":"ignored"}]}"""
        )
        assertEquals("x", tasks.tasks.single().title)
    }

    @Test
    fun `a task with null title does not crash the decoder`() {
        // Title is non-nullable with a default; an explicit null falls back.
        val tasks = json.decodeFromString<GoogleTasksResponse>(
            """{"read_succeeded":true,"tasks":[{"id":"1","title":null}]}"""
        )
        assertEquals("", tasks.tasks.single().title)
        assertEquals("(untitled task)", tasks.tasks.single().displayTitle)
    }
}
