package com.vantage.companion.data.api

import java.io.ByteArrayInputStream
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * The pinning contract, proven across languages.
 *
 * The reference fingerprint below was computed for
 * `resources/pinning-test-cert.pem` three independent ways:
 *   - `openssl x509 -fingerprint -sha256`
 *   - the backend's `jarvis/companion/cert.fingerprint` (Python)
 *   - this test's `PinnedCertificateTrustManager.fingerprintOf` (Kotlin/Java)
 * All three must agree, or a phone pinning the served value would reject the
 * genuine Mac.
 */
class PinnedCertificateTrustManagerTest {

    /** openssl x509 -in pinning-test-cert.pem -noout -fingerprint -sha256 */
    private val goodReference = "3DF3 CB13 C0F3 7603 15C8 6D71 DA91 1286 BA69 E648 421D 4136 348F 0DDF E9E3 E435"
    /** openssl x509 -in pinning-test-wrong.pem -noout -fingerprint -sha256 */
    private val wrongReference = "4704 6BBE 02A2 CD19 4AA4 C5D4 EEFA 7535 2804 00C3 158C 5C0A 8913 6A05 4385 AB63"

    private fun cert(resource: String): X509Certificate {
        val stream = javaClass.classLoader.getResourceAsStream(resource)
            ?: error("test resource $resource not on classpath")
        return CertificateFactory.getInstance("X.509")
            .generateCertificate(ByteArrayInputStream(stream.readBytes())) as X509Certificate
    }

    @Test
    fun fingerprint_matches_openssl_and_backend() {
        val good = cert("pinning-test-cert.pem")
        val wrong = cert("pinning-test-wrong.pem")
        // Kotlin/Java side must equal the openssl/Python reference values.
        assertEquals(goodReference, PinnedCertificateTrustManager.fingerprintOf(good))
        assertEquals(wrongReference, PinnedCertificateTrustManager.fingerprintOf(wrong))
    }

    @Test
    fun normalize_accepts_person_typed_variants() {
        val canonical = "3DF3 CB13 C0F3 7603 15C8 6D71 DA91 1286 BA69 E648 421D 4136 348F 0DDF E9E3 E435"
        assertEquals(canonical, PinnedCertificateTrustManager.normalize(goodReference))
        // A person (or a QR scanner) may hand over colons or dashes or lowercase.
        assertEquals(canonical, PinnedCertificateTrustManager.normalize(goodReference.replace(" ", ":")))
        assertEquals(canonical, PinnedCertificateTrustManager.normalize(goodReference.replace(" ", "-")))
        assertEquals(canonical, PinnedCertificateTrustManager.normalize(goodReference.replace(" ", "").lowercase()))
    }

    @Test
    fun normalize_rejects_garbage() {
        val bad = listOf("", "3DF3", "not hex at all 000000000000000000000000000000000000000000000000000000000000")
        bad.forEach {
            try {
                PinnedCertificateTrustManager.normalize(it)
                fail("expected rejection of: $it")
            } catch (expected: IllegalArgumentException) {
                // ok
            }
        }
    }

    @Test
    fun server_trust_accepts_the_pinned_certificate() {
        val tm = PinnedCertificateTrustManager(goodReference)
        val good = cert("pinning-test-cert.pem")
        // Must not throw.
        tm.checkServerTrusted(arrayOf(good), "RSA")
    }

    @Test
    fun server_trust_rejects_a_different_certificate() {
        val tm = PinnedCertificateTrustManager(goodReference)
        val wrong = cert("pinning-test-wrong.pem")
        try {
            tm.checkServerTrusted(arrayOf(wrong), "RSA")
            fail("a non-pinned certificate must be rejected")
        } catch (expected: java.lang.SecurityException) {
            // The message deliberately does not echo either fingerprint.
            assertTrue(expected.message!!.isNotBlank())
        }
    }

    @Test
    fun server_trust_rejects_empty_chain() {
        val tm = PinnedCertificateTrustManager(goodReference)
        try {
            tm.checkServerTrusted(emptyArray(), "RSA")
            fail("an empty chain must be rejected")
        } catch (expected: java.lang.SecurityException) {
            // ok
        }
    }

    @Test
    fun accepted_issuers_are_empty() {
        // Server-auth only: no client certificates are accepted.
        assertEquals(0, PinnedCertificateTrustManager(goodReference).acceptedIssuers.size)
    }
}
