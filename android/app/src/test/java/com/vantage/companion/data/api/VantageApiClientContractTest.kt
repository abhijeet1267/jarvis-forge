package com.vantage.companion.data.api

import com.vantage.companion.data.local.InMemorySharedPreferences
import com.vantage.companion.data.local.SessionStore
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Client request contract: what actually goes over the wire to the backend /
 * companion gateway (paths, headers, bodies), verified by recording requests
 * on a MockWebServer. The legacy web endpoints (`/phone` bootstrap,
 * `/auth/session`) are deliberately absent from the native client — they are
 * same-origin-protected in the backend, so a native app can never use them.
 */
class VantageApiClientContractTest {

    private val server = MockWebServer()

    @Before
    fun setUp() { server.start() }

    @After
    fun tearDown() { runCatching { server.shutdown() } }

    private fun storeWith(url: String, token: String? = null): SessionStore {
        val store = SessionStore(InMemorySharedPreferences(), InMemorySharedPreferences())
        store.saveBackendURL(url)
        if (token != null) store.saveSession(token, "phone")
        return store
    }

    private fun client(url: String, token: String? = null): VantageApiClient =
        VantageApiClient(storeWith(url, token))

    private fun responseFrom(body: String, code: Int = 200): MockResponse =
        MockResponse().setResponseCode(code)
            .setHeader("Content-Type", "application/json").setBody(body)

    // ------------------------------------------------------------------
    // Gateway pairing
    // ------------------------------------------------------------------

    @Test
    fun redeem_posts_code_to_companion_gateway() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'))
        server.enqueue(responseFrom(
            """{"session_token":"CMP_new","surface":"phone","expires_in":28800}"""))
        val session = c.redeemPairingCode("AB12CD")
        assertEquals("CMP_new", session.sessionToken)
        assertEquals("phone", session.surface)

        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/companion/pairing-redeem", req.path)
        val body = Json.parseToJsonElement(
            req.body?.readUtf8().orEmpty()
        ) as kotlinx.serialization.json.JsonObject
        assertEquals("AB12CD", (body["code"] as JsonPrimitive).content)
    }

    @Test
    fun session_token_header_is_sent_when_present() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'), token = "CMP_tok_123")
        server.enqueue(responseFrom("""{"status":"ok","tools":7}"""))
        c.fetchHealth()
        val recorded = server.takeRequest()
        assertEquals("CMP_tok_123", recorded.getHeader("X-JARVIS-Token"))
        assertEquals("/health", recorded.path)
    }

    @Test
    fun sign_out_deletes_companion_session() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'), token = "CMP_tok_123")
        server.enqueue(responseFrom("""{"status":"signed_out"}"""))
        c.signOut()
        val recorded = server.takeRequest()
        assertEquals("DELETE", recorded.method)
        assertEquals("/companion/session", recorded.path)
        assertEquals("CMP_tok_123", recorded.getHeader("X-JARVIS-Token"))
    }

    // ------------------------------------------------------------------
    // Phone surface — the paths the gateway proxies
    // ------------------------------------------------------------------

    @Test
    fun send_command_posts_real_request_shape() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'))
        server.enqueue(responseFrom(
            """{"reply":"pong","request_id":"REQ_1","decision":"EXECUTE","needs_confirmation":false,"actions":[]}"""))
        val resp = c.sendCommand(CommandRequest(text = "ping", deviceId = "phone_test", source = "mobile"))
        assertEquals("pong", resp.reply)

        val recorded = server.takeRequest()
        assertEquals("POST", recorded.method)
        assertEquals("/command", recorded.path)
        val obj = Json.parseToJsonElement(
            recorded.body?.readUtf8().orEmpty()
        ) as kotlinx.serialization.json.JsonObject
        assertEquals("ping", (obj["text"] as JsonPrimitive).content)
        assertEquals("phone_test", (obj["device_id"] as JsonPrimitive).content)
        assertEquals("mobile", (obj["source"] as JsonPrimitive).content)
    }

    @Test
    fun approvals_use_confirm_path_with_query() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'))
        // approveConfirmation POSTs /confirm/CONF_1?approve=true, then the client
        // refreshes the pending list with a follow-up GET /confirmations. Enqueue
        // both responses (the refresh would otherwise block runTest waiting for a
        // reply that is never enqueued).
        server.enqueue(responseFrom("""{"reply":"Approved.","decision":"EXECUTE","actions":[]}"""))
        server.enqueue(responseFrom("""[]"""))
        c.approveConfirmation("CONF_1")
        // The path carries `approve` AND the approver's session. The session is
        // what the backend's self-approval guard compares against the session
        // that raised the request, so a phone cannot approve its own request
        // by answering with an empty identity. Asserted on the parts, not on
        // the whole string, so adding a parameter is not a failure but losing
        // one is.
        val approvePath = server.takeRequest().path!!
        assertTrue(
            approvePath.startsWith("/confirm/CONF_1?"),
        ) // unexpected confirm path: $approvePath
        assertTrue(
            approvePath.contains("approve=true"),
        ) // the approve call does not say it approved: $approvePath
        assertTrue(
            approvePath.contains("session_id="),
        ) // the approve call does not carry the approver's session: $approvePath
        assertEquals("/confirmations", server.takeRequest().path)

        server.enqueue(responseFrom("""{"reply":"Denied.","decision":"EXECUTE","actions":[]}"""))
        server.enqueue(responseFrom("""[]"""))
        c.denyConfirmation("CONF_1")
        val denyPath = server.takeRequest().path!!
        assertTrue(
            denyPath.startsWith("/confirm/CONF_1?"),
        ) // unexpected confirm path: $denyPath
        assertTrue(
            denyPath.contains("approve=false"),
        ) // the deny call does not say it denied: $denyPath
        assertTrue(
            denyPath.contains("session_id="),
        ) // the deny call does not carry the approver's session: $denyPath
        assertEquals("/confirmations", server.takeRequest().path)
    }

    @Test
    fun fetch_confirmations_hits_phone_surface() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'))
        server.enqueue(responseFrom("""[]"""))
        c.fetchConfirmations()
        // One request is made; takeRequest() once (it blocks until a request
        // arrives, so calling it twice would wait forever for a second request
        // that never comes).
        val recorded = server.takeRequest()
        assertEquals("GET", recorded.method)
        assertEquals("/confirmations", recorded.path)
    }

    @Test
    fun fetch_devices_hits_phone_surface() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'))
        server.enqueue(responseFrom("""[]"""))
        c.fetchDevices()
        val recorded = server.takeRequest()
        assertEquals("GET", recorded.method)
        assertEquals("/devices", recorded.path)
    }

    @Test
    fun pair_request_posts_to_phone_surface() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'))
        server.enqueue(responseFrom("""{"pairing_id":"PR_1","state":"pending"}"""))
        c.requestPairing("phone_test", "Test Phone")
        val recorded = server.takeRequest()
        assertEquals("POST", recorded.method)
        assertEquals("/devices/pair/request", recorded.path)
    }

    @Test
    fun emergency_stop_posts_with_no_body() = runTest {
        val c = client(server.url("/").toString().trimEnd('/'))
        server.enqueue(responseFrom(
            """{"ok":true,"verified":true,"result":{"message":"stopped","physical_stop":"not_available"}}"""))
        val r = c.emergencyStop()
        assertTrue(r.ok == true)
        // One request made -> one takeRequest(); a second call would block forever.
        val recorded = server.takeRequest()
        assertEquals("POST", recorded.method)
        assertEquals("/emergency_stop", recorded.path)
    }

    // ------------------------------------------------------------------
    // URL building
    // ------------------------------------------------------------------

    @Test
    fun url_building_defaults_to_gateway_https() {
        val c = client("192.168.1.50")
        // A bare host means the TLS gateway, not the plain backend.
        val url = c.buildUrl("/health")
        assertEquals("https", url.scheme)
        assertEquals("192.168.1.50", url.host)
        assertEquals(8766, url.port)
        assertEquals("/health", url.encodedPath)
    }

    @Test
    fun url_building_honors_explicit_port_and_scheme() {
        val c = client("http://127.0.0.1:18766")
        val url = c.buildUrl("/companion/pairing-redeem")
        assertEquals("http", url.scheme)
        assertEquals(18766, url.port)
        assertEquals("/companion/pairing-redeem", url.encodedPath)
    }

    @Test
    fun unauthenticated_request_clears_local_state() = runTest {
        val store = storeWith(server.url("/").toString().trimEnd('/'), token = "CMP_dead")
        val c = VantageApiClient(store)
        server.enqueue(responseFrom("""{"detail":"Missing or invalid API token."}""", 401))
        try {
            c.fetchHealth()
        } catch (expected: ApiException.Unauthorized) {
            // the point under test:
        }
        // A 401 means the session died; the app must return to setup, so the
        // token is cleared locally rather than retried into a loop.
        assertEquals(null, store.sessionToken())
    }
}
