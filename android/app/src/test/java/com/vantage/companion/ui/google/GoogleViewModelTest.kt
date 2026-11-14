package com.vantage.companion.ui.google

import com.vantage.companion.data.api.GoogleConnection
import com.vantage.companion.data.api.GoogleTask
import com.vantage.companion.data.api.VantageApiClient
import com.vantage.companion.data.local.InMemorySharedPreferences
import com.vantage.companion.data.local.SessionStore
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The Google screen's behavior, driven through the real HTTP client against a
 * MockWebServer: what the phone re-reads, what it refuses to claim, and what
 * it says when the backend is unreachable.
 *
 * The ViewModel dispatches its work on Dispatchers.IO; the main dispatcher is
 * replaced with an unconfined test dispatcher so viewModelScope starts work
 * immediately, and every call is awaited on the server side (takeRequest)
 * rather than slept on.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class GoogleViewModelTest {

    private val server = MockWebServer()
    private lateinit var store: SessionStore
    private lateinit var client: VantageApiClient
    private var vm: GoogleViewModel? = null

    @Before
    fun setUp() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        server.start()
        store = SessionStore(InMemorySharedPreferences(), InMemorySharedPreferences())
        store.saveBackendURL(server.url("/").toString().trimEnd('/'))
        store.saveSession("CMP_test", "phone")
        client = VantageApiClient(store)
    }

    @After
    fun tearDown() {
        vm = null
        runCatching { server.shutdown() }
        Dispatchers.resetMain()
    }

    // -- fixtures ---------------------------------------------------------

    private fun connectedStatus() = """{
        "connection": {"state":"connected","detail":"Connected to Google.",
          "account":"owner@example.com","scopes":["tasks"],
          "configured":true,"has_token":true,"usable":true},
        "setup": {"configured":true,"setup":[]}
    }"""

    private fun tasksBody(vararg titles: Pair<String, Boolean>) = """{
        "provider":"google_tasks","status":"has_results","read_succeeded":true,
        "detail":null,"count":${titles.size},"tasks":[
          ${titles.joinToString(",") { (t, done) ->
            """{"id":"${t.hashCode()}","title":"$t","done":$done,"status":"needsAction"}"""
          }}
        ]
    }"""

    private fun eventsBody() = """{
        "provider":"google","status":"real_empty","read_succeeded":true,
        "detail":null,"count":0,"events":[]
    }"""

    private fun json(body: String) = MockResponse().setResponseCode(200)
        .setHeader("Content-Type", "application/json").setBody(body)

    /**
     * Route by path. A queued dispatcher would be order-dependent; the paths
     * here are deterministic and each handler can be swapped per test.
     */
    private fun route(
        status: () -> MockResponse,
        tasks: (RecordedRequest) -> MockResponse,
        events: () -> MockResponse = { json(eventsBody()) },
    ) {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path.orEmpty()
                return when {
                    path.startsWith("/google/status") -> status()
                    path.startsWith("/google/tasks") -> tasks(request)
                    path.startsWith("/google/calendar/events") -> events()
                    path.startsWith("/command") -> json(
                        """{"reply":"I need your approval before writing to Google.",
                            "decision":"CONFIRM","needs_confirmation":true,
                            "confirmation_id":"CONF_1","actions":[]}"""
                    )
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
    }

    /** Wait (bounded) until [predicate] holds, draining work on the server. */
    private fun awaitUntil(timeoutMs: Long = 5_000, predicate: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate()) return
            Thread.sleep(20)
        }
        throw AssertionError("condition not reached within ${timeoutMs}ms")
    }

    private fun newViewModel(): GoogleViewModel =
        GoogleViewModel(client, store).also { vm = it }

    private fun state() = vm!!.state.value

    // -- tests ------------------------------------------------------------

    @Test
    fun `refresh loads the connected status, events and tasks`() {
        route(
            status = { json(connectedStatus()) },
            tasks = { json(tasksBody("Buy milk" to false)) },
            events = { json(eventsBody()) },
        )
        newViewModel()
        awaitUntil { !state().isLoading && state().tasks.isNotEmpty() }
        assertTrue(state().connected)
        assertEquals(listOf("Buy milk"), state().tasks.map { it.title })
        assertFalse(state().tasksFailed)
        assertNull(state().error)
    }

    @Test
    fun `a refresh after an approval on the Mac picks up the new task`() {
        var tasks: Pair<String, Boolean> = "Buy milk" to false
        route(
            status = { json(connectedStatus()) },
            tasks = { json(tasksBody(tasks)) },
        )
        newViewModel()
        awaitUntil { state().tasks.isNotEmpty() }

        // While the screen was away, the Mac approved the add: Google now
        // holds two tasks. Returning to the screen (or pull-to-refresh) must
        // re-read rather than keep showing the stale list.
        tasks = "Buy milk" to false
        route(
            status = { json(connectedStatus()) },
            tasks = { json(tasksBody("Buy milk" to false, "Call the dentist" to false)) },
        )
        vm!!.refreshQuietly()
        awaitUntil { state().tasks.size == 2 }
        assertEquals(listOf("Buy milk", "Call the dentist"), state().tasks.map { it.title })
    }

    @Test
    fun `a write that asks for approval does not tick the row or invent a task`() {
        route(
            status = { json(connectedStatus()) },
            tasks = { json(tasksBody("Buy milk" to false)) },
        )
        newViewModel()
        awaitUntil { state().tasks.isNotEmpty() }

        vm!!.completeTask(GoogleTask(id = "1", title = "Buy milk"))
        awaitUntil { state().pendingApproval != null && !state().isWriting }
        assertNotNull(state().pendingApproval)
        // Nothing was written yet: the row is still open and no new task appeared.
        assertFalse(state().tasks.first().done)
        assertEquals(1, state().tasks.size)
    }

    @Test
    fun `a done task is never asked to complete again`() {
        route(
            status = { json(connectedStatus()) },
            tasks = { json(tasksBody("Done thing" to true)) },
        )
        newViewModel()
        awaitUntil { !state().isLoading }
        val before = server.requestCount
        vm!!.completeTask(GoogleTask(id = "1", title = "Done thing", done = true))
        // No /command may be issued. Give any (wrong) request a moment.
        Thread.sleep(250)
        assertEquals(before, server.requestCount)
        assertNull(state().pendingApproval)
    }

    @Test
    fun `addTask sends the Google sentence with mobile source and session`() {
        route(
            status = { json(connectedStatus()) },
            tasks = { json(tasksBody()) },
        )
        newViewModel()
        awaitUntil { !state().isLoading }
        vm!!.addTask("  Buy milk  ")
        val cmd = awaitCommand()
        val obj = kotlinx.serialization.json.Json
            .parseToJsonElement(cmd.body?.readUtf8().orEmpty())
        assertTrue(obj.toString().contains("add a task to Google Tasks Buy milk"))
        assertTrue(obj.toString().contains("\"source\":\"mobile\""))
        assertTrue(obj.toString().contains("\"session_id\":\"CMP_test\""))
    }

    /** Take requests until the /command one arrives; returns it. */
    private fun awaitCommand(): RecordedRequest {
        val deadline = System.currentTimeMillis() + 5_000
        while (System.currentTimeMillis() < deadline) {
            val r = server.takeRequest(1, TimeUnit.SECONDS) ?: continue
            if (r.path.orEmpty().startsWith("/command")) return r
        }
        throw AssertionError("no /command request was issued")
    }

    @Test
    fun `addTask rejects blank and whitespace titles client-side`() {
        route(
            status = { json(connectedStatus()) },
            tasks = { json(tasksBody()) },
        )
        newViewModel()
        awaitUntil { !state().isLoading }
        val before = server.requestCount
        vm!!.addTask("   ")
        vm!!.addTask("")
        Thread.sleep(250)
        assertEquals(before, server.requestCount)
        assertFalse(state().isWriting)
    }

    @Test
    fun `an outage shows a retryable error and no spinner, and retry recovers`() {
        var down = true
        route(
            status = {
                if (down) MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START)
                else json(connectedStatus())
            },
            tasks = { json(tasksBody("Buy milk" to false)) },
        )
        newViewModel()
        awaitUntil { !state().isLoading }
        // Status is unknown and the screen says so with a message, not a spinner.
        assertTrue(state().statusUnknown)
        assertNotNull(state().error)

        down = false
        vm!!.refresh()
        awaitUntil { state().connected && state().tasks.isNotEmpty() }
        assertNull(state().error)
        assertFalse(state().statusUnknown)
    }

    @Test
    fun `a failed task read is a failure, not an empty list`() {
        route(
            status = { json(connectedStatus()) },
            tasks = {
                json("""{"read_succeeded":false,"count":0,"tasks":[],
                        "detail":"Google rejected the access token."}""")
            },
        )
        newViewModel()
        awaitUntil { state().tasksFailed }
        assertTrue(state().tasksFailed)
        assertTrue(state().tasks.isEmpty())
        assertEquals("Google rejected the access token.", state().tasksDetail)
    }

    @Test
    fun `setShowCompleted re-reads tasks with the toggle applied`() {
        var lastPath = ""
        route(
            status = { json(connectedStatus()) },
            tasks = { request ->
                lastPath = request.path.orEmpty()
                json(tasksBody("Buy milk" to false))
            },
        )
        newViewModel()
        awaitUntil { state().tasks.isNotEmpty() }
        vm!!.setShowCompleted(true)
        awaitUntil { lastPath.contains("show_completed=true") }
        assertTrue(state().showCompleted)
    }

    @Test
    fun `a second refresh while one is running does not leave the UI stuck loading`() {
        route(
            status = { json(connectedStatus()) },
            tasks = { json(tasksBody()) },
        )
        newViewModel()
        vm!!.refresh()
        vm!!.refresh()
        vm!!.refresh()
        awaitUntil { !state().isLoading }
        assertFalse(state().isLoading)
    }

    @Test
    fun `the connection card renders states as truthful labels`() {
        assertEquals("Connected to Google",
            GoogleConnection(state = "connected", usable = true).stateLabel)
        assertEquals("Google is not set up",
            GoogleConnection(state = "not_configured").stateLabel)
        assertEquals("Google status unavailable",
            GoogleConnection().stateLabel)
        assertEquals("Google status: something_new",
            GoogleConnection(state = "something_new").stateLabel)
    }
}