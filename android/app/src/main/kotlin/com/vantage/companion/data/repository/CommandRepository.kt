package com.vantage.companion.data.repository

import com.vantage.companion.data.api.CommandRequest
import com.vantage.companion.data.api.CommandResponse
import com.vantage.companion.data.api.VantageApiClient
import com.vantage.companion.data.local.SessionStore

class CommandRepository(
    private val api: VantageApiClient,
    private val store: SessionStore,
) {
    suspend fun sendCommand(text: String): CommandResponse {
        return api.sendCommand(
            CommandRequest(
                text = text,
                deviceId = store.deviceId.value.ifBlank { "phone" },
                source = "mobile",
                // The session the app holds, so the backend knows which device
                // raised the request.
                //
                // It used to be null, and the backend then minted a fresh
                // SESS-... id for every request. That made the request
                // unattributable: the backend's self-approval guard compares
                // the session that raised a request with the session that
                // answers it, and with a fresh id on every call the two could
                // never be the same -- so a phone could raise a request and
                // approve it, and the guard had no way to tell.
                sessionId = store.sessionToken(),
                context = emptyMap(),
            )
        )
    }
}