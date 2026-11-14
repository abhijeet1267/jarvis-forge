package com.vantage.companion.data.api

import java.security.MessageDigest
import java.security.cert.X509Certificate
import javax.net.ssl.X509TrustManager

/**
 * Pins the companion gateway's self-signed certificate by its standard X.509
 * SHA-256 fingerprint — the exact value the backend serves in
 * `certificate_fingerprint` (QR code / manual entry) and prints at startup.
 *
 * The check: during the TLS handshake the leaf certificate's DER encoding is
 * hashed and compared to the expected fingerprint. A MITM presenting any other
 * certificate (including a different self-signed one) fails the handshake
 * before a single byte of app data is exchanged.
 *
 * Hostname verification is not applied on top: the certificate's subject is
 * the Mac's host name while the user connects by LAN IP, so name and IP
 * legitimately differ. Pinning the exact certificate is a stronger identity
 * guarantee than a host name match — a MITM would have to present precisely
 * the pinned certificate, which only the Mac holds.
 */
class PinnedCertificateTrustManager(
    expectedFingerprint: String,
) : X509TrustManager {

    companion object {
        /**
         * Normalize a fingerprint to uppercase, space-grouped hex (the backend's
         * display format: "3DF3 CB13 ..."). Accepts colons, dashes, spaces, or
         * a bare 64-char hex string as typed by a person.
         */
        fun normalize(fingerprint: String): String {
            val hex = fingerprint
                .replace(" ", "")
                .replace(":", "")
                .replace("-", "")
                .uppercase()
            if (hex.length != 64 || hex.any { it !in "0123456789ABCDEF" }) {
                throw IllegalArgumentException(
                    "expected a 64-hex-char SHA-256 fingerprint, got ${hex.length} chars"
                )
            }
            return hex.chunked(4).joinToString(" ")
        }

        /** SHA-256 of a certificate's DER encoding, in the backend's display format. */
        fun fingerprintOf(cert: X509Certificate): String {
            val sha = MessageDigest.getInstance("SHA-256").digest(cert.encoded)
            val hex = sha.joinToString("") { "%02X".format(it) }
            return hex.chunked(4).joinToString(" ")
        }
    }

    private val expected: String = normalize(expectedFingerprint)

    /** Thrown when the presented certificate is not the pinned one. */
    class MismatchException :
        java.lang.SecurityException("certificate fingerprint mismatch")

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        // Server-auth only: the phone never presents a certificate.
    }

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        if (chain.isNullOrEmpty()) throw SecurityException("empty certificate chain")
        val leaf = chain[0]
        if (fingerprintOf(leaf) != expected) {
            // Do not echo either value: the error travels over the network we
            // just decided not to trust.
            throw MismatchException()
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> =
        // Used by the platform for client-auth chains, which we don't do.
        emptyArray()
}
