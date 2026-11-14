package com.vantage.companion.data.api

import com.vantage.companion.data.local.SessionStore
import okhttp3.Interceptor
import okhttp3.Response

class AuthInterceptor(
    private val sessionStore: SessionStore,
) : Interceptor {

    companion object {
        const val TOKEN_HEADER = "X-JARVIS-Token"
        const val USER_AGENT = "VantageCompanion/1.0 Android"

        /**
         * Which handset this is.
         *
         * The companion gateway forwards this header to the backend, and the
         * backend reads it as the device the request came from -- so a command
         * raised on a phone is recorded as a phone's command, and the audit
         * chain says which device it was. Without it the gateway forwarded
         * nothing and the backend had no way to know.
         */
        const val DEVICE_ID_HEADER = "X-Vantage-Device-Id"
    }

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request().newBuilder().apply {
            header("User-Agent", USER_AGENT)
            header("Accept", "application/json")
            sessionStore.sessionToken()?.let { token ->
                header(TOKEN_HEADER, token)
            }
            // The device id travels with every request, so the gateway can
            // forward it and the backend can attribute the request to the
            // handset that raised it.
            sessionStore.deviceId.value.takeIf { it.isNotBlank() }?.let { deviceId ->
                header(DEVICE_ID_HEADER, deviceId)
            }
        }.build()

        return chain.proceed(request)
    }
}