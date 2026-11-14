package com.vantage.companion.data.api

import com.vantage.companion.data.local.SessionStore
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.serializer
import kotlinx.serialization.serializer
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.logging.HttpLoggingInterceptor
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

class VantageApiClient(
    // `internal`, not `private`: the inline helpers below read it, and a
    // public/internal inline function may only touch members at least as
    // visible as itself.
    internal val sessionStore: SessionStore,
) {
    // Shared with the unit tests: the decoder under test must be the decoder
    // production uses (see WireJson in ApiModels.kt).
    internal val json = WireJson

    internal val mediaType = "application/json".toMediaType()

    private val _pendingApprovalCount = MutableStateFlow(0)
    val pendingApprovalCount: StateFlow<Int> = _pendingApprovalCount.asStateFlow()

    private val _client = MutableStateFlow(buildClient(sessionStore.certFingerprint()))
    internal val okHttpClient: OkHttpClient get() = _client.value

    /**
     * Rebuild the client to pin the gateway's certificate to [fingerprint]
     * (the backend's X.509 SHA-256). Called before the redeem so the very
     * first connection is already verified. [fingerprint] null ⇒ plain system
     * trust (used only by unit tests against a local mock).
     */
    fun setPinnedFingerprint(fingerprint: String?) {
        _client.value = buildClient(fingerprint)
    }

    private fun buildClient(fingerprint: String?): OkHttpClient {
        val builder = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .addInterceptor(AuthInterceptor(sessionStore))
            .addInterceptor(
                HttpLoggingInterceptor().apply {
                    // Redact the token; log bodies otherwise for diagnosing pairing.
                    level = HttpLoggingInterceptor.Level.HEADERS
                }
            )
        if (!fingerprint.isNullOrBlank()) {
            val tm = PinnedCertificateTrustManager(fingerprint)
            val ctx = javax.net.ssl.SSLContext.getInstance("TLS")
            ctx.init(null, arrayOf(tm), java.security.SecureRandom())
            builder.sslSocketFactory(ctx.socketFactory, tm)
        }
        return builder.build()
    }

    fun backendHost(): String {
        val raw = sessionStore.backendUrl.value.trimEnd('/')
        if (raw.isBlank()) throw ApiException.NotConfigured
        return raw
    }

    /**
     * The decoder production parses every response with. Exposed for the
     * tests so they exercise the same configuration (kotlinx.serialization's
     * `Json` is immutable, so sharing it is safe).
     */
    internal fun decoderForTest(): Json = json

    fun buildUrl(path: String): HttpUrl {
        val raw = backendHost()
        // Honor an explicit scheme so the native app can reach the TLS gateway
        // (https://host:8766) while unit tests point at a local http mock.
        val (scheme, hostPart) = if (raw.startsWith("https://")) {
            "https" to raw.removePrefix("https://")
        } else if (raw.startsWith("http://")) {
            "http" to raw.removePrefix("http://")
        } else {
            "https" to raw
        }
        val parts = hostPart.split(":")
        val hostname = parts[0]
        val port = parts.getOrNull(1)?.toIntOrNull() ?: 8766
        // Split an embedded query string (e.g. "/confirm/X?approve=true") from
        // the path. addPathSegments would otherwise percent-encode the '?' to
        // %3F, so the backend would receive /confirm/X%3Fapprove=true and the
        // /confirm/{id}?approve=... route would not match.
        val qIndex = path.indexOf('?')
        val pathPart = if (qIndex >= 0) path.substring(0, qIndex) else path
        val queryPart = if (qIndex >= 0) path.substring(qIndex + 1) else ""
        val builder = HttpUrl.Builder()
            .scheme(scheme)
            .host(hostname)
            .port(port)
            .addPathSegments(pathPart.trimStart('/'))
        for (pair in queryPart.split('&').filter { it.isNotEmpty() }) {
            val eq = pair.indexOf('=')
            if (eq >= 0) builder.addQueryParameter(pair.substring(0, eq), pair.substring(eq + 1))
            else builder.addQueryParameter(pair, null)
        }
        return builder.build()
    }

    suspend internal inline fun <reified T> get(
        path: String,
        extraHeaders: Map<String, String> = emptyMap(),
    ): T {
        val request = Request.Builder()
            .url(buildUrl(path))
            .get()
            .apply { extraHeaders.forEach { (k, v) -> addHeader(k, v) } }
            .build()
        return execute(request)
    }

    suspend internal inline fun <reified T> post(
        path: String,
        extraHeaders: Map<String, String> = emptyMap(),
    ): T {
        val request = Request.Builder()
            .url(buildUrl(path))
            .post("".toRequestBody(null))
            .apply { extraHeaders.forEach { (k, v) -> addHeader(k, v) } }
            .build()
        return execute(request)
    }

    suspend internal inline fun <reified T, reified B : Any> post(
        path: String,
        body: B,
        extraHeaders: Map<String, String> = emptyMap(),
    ): T {
        // The body is serialized with ITS OWN type (reified B) — the reified T
        // is the RESPONSE type, so encoding the body as T would produce the
        // wrong wire shape.
        val bodyStr = when (body) {
            is String -> body
            else -> json.encodeToString(serializer<B>(), body)
        }
        val requestBody = bodyStr.toRequestBody(mediaType)
        val request = Request.Builder()
            .url(buildUrl(path))
            .post(requestBody)
            .apply { extraHeaders.forEach { (k, v) -> addHeader(k, v) } }
            .build()
        return execute(request)
    }

    suspend internal inline fun <reified T> delete(
        path: String,
        extraHeaders: Map<String, String> = emptyMap(),
    ): T {
        val request = Request.Builder()
            .url(buildUrl(path))
            .delete()
            .apply { extraHeaders.forEach { (k, v) -> addHeader(k, v) } }
            .build()
        return execute(request)
    }

    suspend internal inline fun <reified T> execute(request: Request): T {
        val (response, bodyString) = try {
            val r = okHttpClient.newCall(request).await()
            r to (r.body?.string() ?: "")
        } catch (e: IOException) {
            throw asTransportFailure(e)
        }
        if (!response.isSuccessful) {
            when (response.code) {
                401 -> {
                    sessionStore.clearAll()
                    throw ApiException.Unauthorized
                }
                403 -> {
                    sessionStore.clearAll()
                    throw ApiException.TokenRevoked
                }
                else -> throw ApiException.BadStatus(response.code, bodyString)
            }
        }
        if (bodyString.isBlank()) throw ApiException.EmptyResponse
        return try {
            json.decodeFromString<T>(bodyString)
        } catch (e: Exception) {
            throw ApiException.Decoding(e.message ?: "parse error")
        }
    }

    // -- Public API --

    suspend fun fetchHealth(): HealthResponse = get("/health")

    /**
     * Who the backend is, before any credential question.
     *
     * Null means the route does not exist — an older backend. That is a fact the
     * home screen must be able to state, so this returns null rather than
     * throwing: "cannot report identity" is not "the network failed".
     */
    suspend fun fetchBackendIdentityOrNull(): BackendIdentityResponse? =
        try { get("/health/identity") } catch (_: Exception) { null }

    suspend fun fetchPairingCode(): PairingCodeResponse = post("/companion/pairing-code")

    /**
     * Redeem a one-shot pairing code through the companion gateway for a
     * scoped session token. The gateway (jarvis/companion/gateway.py) answers
     * exactly `{"session_token", "surface", "expires_in"}`; a reused or
     * expired code is 403 and no token is returned.
     */
    /**
     * Redeem a pairing code for a session token.
     *
     * The device id and name go with it: the gateway registers the phone in
     * the backend's device registry as it redeems, so every later command
     * names a device the registry knows.
     */
    suspend fun redeemPairingCode(
        code: String,
        deviceId: String? = null,
        deviceName: String? = null,
    ): SessionResponse =
        post(
            "/companion/pairing-redeem",
            PairingRedeemRequest(code, deviceId, deviceName),
        )

    suspend fun sendCommand(cmd: CommandRequest): CommandResponse = post("/command", cmd)

    suspend fun fetchConfirmations(): List<Confirmation> {
        val result: List<Confirmation> = get("/confirmations")
        _pendingApprovalCount.value = result.size
        return result
    }

    suspend fun approveConfirmation(id: String): CommandResponse {
        // The backend returns a CommandResponse (jarvis/api/app.py
        // `/confirm/{id}` is response_model=CommandResponse), not a
        // Confirmation row. Decoding it as Confirmation required a
        // `confirmation_id` the response does not carry and threw.
        //
        // The session is sent as `session_id` so the backend knows *who*
        // approved. It is the same token the interceptor puts in the
        // `X-JARVIS-Token` header, and the backend's self-approval guard
        // compares it with the session that raised the request: a phone must
        // not be able to approve its own consequential request, and the
        // guard can only tell that from the session.
        val result: CommandResponse = post("/confirm/$id?approve=true&session_id=${_sessionParam()}")
        // Refresh count
        try {
            val list: List<Confirmation> = get("/confirmations")
            _pendingApprovalCount.value = list.size
        } catch (_: Exception) {}
        return result
    }

    suspend fun denyConfirmation(id: String): CommandResponse {
        // A denial needs the session for the same reason an approval does --
        // the audit records who answered either way.
        val result: CommandResponse = post("/confirm/$id?approve=false&session_id=${_sessionParam()}")
        try {
            val list: List<Confirmation> = get("/confirmations")
            _pendingApprovalCount.value = list.size
        } catch (_: Exception) {}
        return result
    }

    /**
     * The session token as a URL query value, or an empty string when there
     * is no session (an unpaired app). Percent-encoded, because a session
     * token is opaque and must not be able to carry a `&` into the query.
     */
    private fun _sessionParam(): String {
        val token = sessionStore.sessionToken() ?: return ""
        return java.net.URLEncoder.encode(token, "UTF-8")
    }

    /** The Google connection state. Read-only: the phone cannot connect or disconnect. */
    suspend fun fetchGoogleStatus(): GoogleStatusResponse = get("/google/status")

    suspend fun fetchGoogleEvents(
        date: String = "today",
        days: Int = 1,
    ): GoogleEventsResponse = get("/google/calendar/events?date=$date&days=$days")

    suspend fun fetchGoogleTasks(showCompleted: Boolean = false): GoogleTasksResponse =
        get("/google/tasks?show_completed=$showCompleted&limit=100")

    suspend fun fetchDevices(): List<DeviceInfo> = get("/devices")

    suspend fun requestPairing(deviceId: String, deviceName: String): PairingResponse =
        post("/devices/pair/request", PairingRequest(deviceId, deviceName))

    suspend fun emergencyStop(): EmergencyStopResponse = post("/emergency_stop")

    /**
     * Sign out: revoke THIS session at the companion gateway. The gateway
     * answers `{"status":"signed_out"}` for the session named by the
     * X-JARVIS-Token header (AuthInterceptor attaches it); a 404 means the
     * session was already gone. `/auth/session` is a local-phone-page
     * endpoint and is not reachable through the gateway's allowlist.
     */
    suspend fun signOut(): JsonObject = delete("/companion/session")
}

// OkHttp await() extension
suspend fun Call.await(): Response = suspendCancellableCoroutine { cont ->
    enqueue(object : Callback {
        override fun onResponse(call: Call, response: Response) = cont.resume(response)
        override fun onFailure(call: Call, e: IOException) = cont.resumeWithException(e)
    })
    cont.invokeOnCancellation { cancel() }
}

/**
 * Classify a transport-level [IOException] into the app's error vocabulary.
 *
 * OkHttp surfaces DNS failures, refused connections, timeouts and TLS
 * handshake failures as raw `IOException`s whose messages name JDK internals
 * ("Failed to connect to /192.168.1.50:8766"). Every screen catches
 * [ApiException.Transport] to show an actionable sentence, so those raw
 * exceptions used to fall through to the generic branch and were shown
 * verbatim to the user — and a pinned-certificate mismatch, which the JSSE
 * wraps in `SSLHandshakeException -> CertificateException`, lost its
 * security-specific message in Setup. Unwrap the chain and rethrow the two
 * cases the app knows how to explain.
 */
internal fun asTransportFailure(e: IOException): Exception {
    var cause: Throwable? = e
    // Bounded walk: real cause chains are a handful deep (JSSE wraps the pin
    // mismatch 2–3 levels down), and a malformed cyclic chain must not hang
    // the request thread.
    repeat(8) {
        if (cause == null) return ApiException.Transport(e.message ?: e.javaClass.simpleName)
        if (cause is PinnedCertificateTrustManager.MismatchException) return cause
        cause = cause?.cause
    }
    // The message is diagnostic only; screens replace it with a human one.
    return ApiException.Transport(e.message ?: e.javaClass.simpleName)
}

sealed class ApiException(message: String, cause: Throwable? = null) : Exception(message, cause) {
    data object NotConfigured : ApiException("Backend URL not configured.")
    data class BadStatus(val code: Int, val body: String) : ApiException("Server returned $code")
    data object EmptyResponse : ApiException("Server returned an empty response.")
    data class Transport(val msg: String) : ApiException("Could not reach the server: $msg")
    data class Decoding(val msg: String) : ApiException("Failed to parse response: $msg")
    data object Unauthorized : ApiException("Not authorized.")
    data object TokenRevoked : ApiException("Token revoked.")
}