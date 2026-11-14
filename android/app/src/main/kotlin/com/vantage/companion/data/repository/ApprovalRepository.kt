package com.vantage.companion.data.repository

import com.vantage.companion.data.api.CommandResponse
import com.vantage.companion.data.api.Confirmation
import com.vantage.companion.data.api.VantageApiClient

class ApprovalRepository(private val api: VantageApiClient) {
    suspend fun fetchConfirmations(): List<Confirmation> = api.fetchConfirmations()
    // /confirm/{id} returns a CommandResponse (the pipeline outcome), not a
    // Confirmation row -- see VantageApiClient.approveConfirmation.
    suspend fun approve(id: String): CommandResponse = api.approveConfirmation(id)
    suspend fun deny(id: String): CommandResponse = api.denyConfirmation(id)
}