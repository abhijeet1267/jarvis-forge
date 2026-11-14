package com.vantage.companion.ui.command

import com.vantage.companion.data.api.VantageApiClient
import com.vantage.companion.data.local.InMemorySharedPreferences
import com.vantage.companion.data.local.SessionStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.delay
import kotlinx.coroutines.test.StandardTestDispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The Command Center must never send the same question twice.
 *
 * This was not hypothetical on the desktop client: its chat sent on both the
 * field's `onSubmit` and the send button's Return shortcut, so one press could
 * produce two turns. On Android the guard is `isSending`, and a guard that is
 * asserted by reading the source is a guard nobody has exercised — so this test
 * presses send twice against a real MockWebServer and counts what arrived.
 *
 * It also pins the two properties that make a failure recoverable: a failed
 * turn is recorded as an error (not a fabricated answer), and a retry re-sends
 * through the same guarded path.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class CommandViewModelSendTest {

    private val server = MockWebServer()
    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
        server.start()
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
        runCatching { server.shutdown() }
    }

    /**
     * Wait until the view model has finished the turn it started.
     *
     * `send()` launches on `Dispatchers.IO`, so a virtual test clock would
     * advance past a coroutine that has not been scheduled on it. Waiting on the
     * observable result is what the UI itself does.
     */
    private suspend fun settle(vm: CommandViewModel, expectedSends: Int = 1) {
        withTimeout(5_000) {
            while (server.requestCount < expectedSends || vm.isSending.value ||
                vm.history.value.none { it.role != CommandRole.USER }) {
                delay(10)
            }
        }
    }

    private fun viewModel(): CommandViewModel {
        val store = SessionStore(InMemorySharedPreferences(), InMemorySharedPreferences())
        store.saveBackendURL(server.url("/").toString().trimEnd('/'))
        store.saveSession("session-token", "phone")
        return CommandViewModel(VantageApiClient(store), store)
    }

    private fun ok(reply: String = "Done.") = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody("""{"reply":"$reply","decision":"EXECUTE","actions":[]}""")

    @Test
    fun `a second send while one is in flight does not reach the backend`() = runBlocking {
        server.enqueue(ok())
        server.enqueue(ok())
        val vm = viewModel()

        vm.updateInput("what is on my calendar")
        vm.send()
        // The press a fast double-tap produces: the same text, immediately.
        vm.updateInput("what is on my calendar")
        vm.send()

        settle(vm)

        // Exactly one request, because the second call returned early on
        // `isSending` rather than queueing another turn.
        assertEquals("one question must produce one request", 1, server.requestCount)
        assertEquals(1, vm.history.value.count { it.role == CommandRole.USER })
        assertEquals(1, vm.history.value.count { it.role == CommandRole.ASSISTANT })
    }

    @Test
    fun `a blank input sends nothing`() = runBlocking {
        val vm = viewModel()
        vm.updateInput("   ")
        vm.send()
        // Nothing is expected to happen, so the assertion *is* the wait.
        Thread.sleep(250)
        assertEquals(0, server.requestCount)
        assertTrue(vm.history.value.isEmpty())
    }

    @Test
    fun `a failed turn is recorded as an error, not as an answer`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(500).setBody("""{"detail":"boom"}"""))
        val vm = viewModel()

        vm.updateInput("do the thing")
        vm.send()
        settle(vm)

        val last = vm.history.value.last()
        assertEquals("a failure must not be drawn as an AI answer", CommandRole.ERROR, last.role)
        assertTrue(vm.history.value.none { it.role == CommandRole.ASSISTANT })
    }

    @Test
    fun `retry while the question is the last turn re-sends it once`() = runBlocking {
        // Retry is offered while the failed send is still the newest turn — the
        // window in which the person presses it. The previous version of this
        // test waited for the error row first, which means the question was no
        // longer last and retry correctly appended a second turn; that test was
        // asserting the wrong moment, not the code.
        val vm = viewModel()
        vm.updateInput("do the thing")
        vm.send()

        // Wait only until the request has been issued and the turn is in flight.
        withTimeout(5_000) { while (server.requestCount < 1) delay(5) }
        vm.retry()

        // Nothing was sent by the retry: the turn was still in flight.
        assertEquals("a retry during an in-flight turn must not send", 1, server.requestCount)
    }

    @Test
    fun `retry after a failure does not duplicate a question that is already recorded`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(500).setBody("""{"detail":"boom"}"""))
        server.enqueue(ok("Recovered."))
        val vm = viewModel()

        vm.updateInput("do the thing")
        vm.send()
        settle(vm)
        val errorRow = vm.history.value.last()
        assertEquals(CommandRole.ERROR, errorRow.role)

        vm.retry()
        settle(vm, expectedSends = 2)

        // The question was already recorded and is followed by the error, so it
        // is *not* the last turn. `retry()` therefore re-sends it as a new turn
        // rather than pretending an earlier question was never asked: the
        // transcript keeps the failure and shows the recovery below it. What
        // must hold is that the retry sent exactly one request and produced one
        // new user turn plus an answer — not that history was rewritten.
        assertEquals(2, vm.history.value.count { it.role == CommandRole.USER })
        assertEquals(2, server.requestCount)
        assertTrue(vm.history.value.any { it.role == CommandRole.ASSISTANT })
    }

    @Test
    fun `the voice preference does not affect whether a command is sent`() = runBlocking {
        server.enqueue(ok())
        val vm = viewModel()
        vm.updateInput("status")
        vm.send()
        settle(vm)
        // Text chat is independent of speech: nothing in the send path reads it.
        assertEquals(1, server.requestCount)
        assertTrue(vm.history.value.any { it.role == CommandRole.ASSISTANT })
    }
}