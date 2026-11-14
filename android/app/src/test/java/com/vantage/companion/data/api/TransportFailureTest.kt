package com.vantage.companion.data.api

import com.vantage.companion.data.local.InMemorySharedPreferences
import com.vantage.companion.data.local.SessionStore
import java.io.IOException
import java.net.SocketTimeoutException
import javax.net.ssl.SSLHandshakeException
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Transport failures must reach the UI as [ApiException.Transport].
 *
 * Every screen has a `catch (e: ApiException.Transport)` branch with an
 * actionable sentence ("Could not reach the laptop"), but the client used to
 * let raw `IOException`s escape, so that branch was dead and users saw JDK
 * text instead. The classification is pinned here, including the TLS
 * pinning mismatch that the JSSE wraps inside the handshake's IOException.
 */
class TransportFailureTest {

    private fun storeWith(url: String): SessionStore {
        val store = SessionStore(InMemorySharedPreferences(), InMemorySharedPreferences())
        store.saveBackendURL(url)
        return store
    }

    @Test
    fun io_failure_is_classified_as_transport() {
        val e = asTransportFailure(IOException("Failed to connect to /192.168.1.50:8766"))
        assertTrue(e is ApiException.Transport)
        assertEquals("Could not reach the server: Failed to connect to /192.168.1.50:8766", e.message)
    }

    @Test
    fun timeout_is_transport() {
        assertTrue(asTransportFailure(SocketTimeoutException("timeout")) is ApiException.Transport)
    }

    @Test
    fun pinned_certificate_mismatch_is_unwrapped_from_the_handshake() {
        val mismatch = PinnedCertificateTrustManager.MismatchException()
        val wrapped = SSLHandshakeException("handshake failed").apply {
            initCause(javax.net.ssl.SSLException("certificate").apply { initCause(mismatch) })
        }
        assertSame(mismatch, asTransportFailure(wrapped))
    }

    @Test
    fun a_cyclic_cause_chain_does_not_loop_forever() {
        // initCause forbids `e.initCause(e)`, but a two-node cycle is legal to
        // construct; the unwrap loop must still terminate.
        val a = IOException("a")
        val b = IOException("b", a)
        a.initCause(b)
        assertTrue(asTransportFailure(a) is ApiException.Transport)
    }

    @Test
    fun unreachable_gateway_throws_transport_from_a_real_socket_call() = runTest {
        // Bind a port, then release it: the client is pointed at an address
        // that refuses connections, so OkHttp raises a real IOException.
        val server = MockWebServer()
        server.start()
        val url = "http://127.0.0.1:${server.port}"
        server.shutdown()

        val client = VantageApiClient(storeWith(url))
        try {
            client.fetchHealth()
            fail("expected the unreachable gateway to fail")
        } catch (e: ApiException.Transport) {
            // The whole point: screens' Transport branch can now fire.
        }
    }
}